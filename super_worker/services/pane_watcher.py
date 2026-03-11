"""Watch tmux pane output and session state files via kqueue for efficient updates."""

import asyncio
import concurrent.futures
import logging
import os
import select
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# One dedicated thread for the single kqueue blocking call.
# Isolated from asyncio's default pool so it never starves capture/send-keys workers.
_KQUEUE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="sw-kqueue",
)


@dataclass
class _Watch:
    """A single watched fd with its dispatch callback."""
    fd: int
    callback: Callable
    callback_arg: str | None   # None for pipe watches, session_name for state watches
    pipe_path: Path | None = None


class PaneWatcher:
    """Watches tmux pane output and state files via a single shared kqueue.

    One asyncio task blocks on kq.control() (in a dedicated 1-thread executor)
    watching all fds simultaneously. Callbacks are called directly on the event
    loop from the asyncio task — no call_soon_threadsafe, no Textual context
    issues. One thread total regardless of session count.
    """

    _active_pipe_dirs: set[Path] = set()

    def __init__(self) -> None:
        self._kq = select.kqueue()
        self._lock = threading.Lock()
        self._fd_to_watch: dict[int, _Watch] = {}
        self._pipe_watches: dict[str, _Watch] = {}
        self._state_watches: dict[str, _Watch] = {}

        self._pipe_dir = Path(tempfile.mkdtemp(prefix="sw-pipes-"))
        PaneWatcher._active_pipe_dirs.add(self._pipe_dir)

        self._running = True
        self._task: asyncio.Task | None = None
        self._cleanup_stale_pipe_dirs()

    # ── Public API ────────────────────────────────────────────────────────────

    def start_watching(self, session_name: str, callback: Callable) -> None:
        """Start pipe-pane for a session. Calls callback() when output arrives."""
        if session_name in self._pipe_watches:
            self.stop_watching(session_name)

        pipe_path = self._pipe_dir / f"{session_name}.pipe"
        self._pipe_dir.mkdir(parents=True, exist_ok=True)
        pipe_path.touch()

        try:
            subprocess.run(
                ["tmux", "pipe-pane", "-t", session_name, "-o", f"cat >> {pipe_path}"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            logger.debug("Failed to start pipe-pane for %s", session_name)
            return

        try:
            fd = os.open(str(pipe_path), os.O_RDONLY)
        except OSError:
            logger.debug("Failed to open pipe file: %s", pipe_path)
            return

        watch = _Watch(fd=fd, callback=callback, callback_arg=None, pipe_path=pipe_path)
        self._register(watch)
        with self._lock:
            self._pipe_watches[session_name] = watch

    def stop_watching(self, session_name: str) -> None:
        """Stop pipe-pane and kqueue watching for a session."""
        with self._lock:
            watch = self._pipe_watches.pop(session_name, None)
        if watch is None:
            return
        self._unregister(watch)
        try:
            subprocess.run(
                ["tmux", "pipe-pane", "-t", session_name],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        if watch.pipe_path:
            try:
                watch.pipe_path.unlink(missing_ok=True)
            except OSError:
                pass

    def is_watching(self, session_name: str) -> bool:
        """Return True if pipe-pane output is currently being watched."""
        with self._lock:
            return session_name in self._pipe_watches

    def start_watching_state(self, session_name: str, callback: Callable) -> None:
        """Watch a session's state file. Calls callback(session_name) on change."""
        if session_name in self._state_watches:
            self.stop_watching_state(session_name)

        from super_worker.constants import SESSION_STATES_DIR
        state_file = SESSION_STATES_DIR / session_name
        SESSION_STATES_DIR.mkdir(parents=True, exist_ok=True)
        state_file.touch()

        try:
            fd = os.open(str(state_file), os.O_RDONLY)
        except OSError:
            logger.debug("Failed to open state file: %s", state_file)
            return

        watch = _Watch(fd=fd, callback=callback, callback_arg=session_name)
        self._register(watch)
        with self._lock:
            self._state_watches[session_name] = watch

    def stop_watching_state(self, session_name: str) -> None:
        """Stop watching a session's state file."""
        with self._lock:
            watch = self._state_watches.pop(session_name, None)
        if watch is None:
            return
        self._unregister(watch)

    def cleanup(self) -> None:
        """Stop all watches and shut down."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        for name in list(self._pipe_watches):
            self.stop_watching(name)
        for name in list(self._state_watches):
            self.stop_watching_state(name)
        try:
            self._kq.close()
        except Exception:
            pass
        PaneWatcher._active_pipe_dirs.discard(self._pipe_dir)
        import shutil
        try:
            shutil.rmtree(self._pipe_dir, ignore_errors=True)
        except Exception:
            pass

    # ── Internal ──────────────────────────────────────────────────────────────

    def _register(self, watch: _Watch) -> None:
        """Add fd to the shared kqueue and start the watcher task if needed."""
        kevent = select.kevent(
            watch.fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE,
        )
        try:
            self._kq.control([kevent], 0, 0)
        except Exception:
            try:
                os.close(watch.fd)
            except OSError:
                pass
            return
        with self._lock:
            self._fd_to_watch[watch.fd] = watch

        # Ensure the watcher asyncio task is running
        try:
            loop = asyncio.get_running_loop()
            if self._task is None or self._task.done():
                self._task = loop.create_task(self._watch_loop())
        except RuntimeError:
            pass

    def _unregister(self, watch: _Watch) -> None:
        """Remove fd from the shared kqueue and close it."""
        with self._lock:
            self._fd_to_watch.pop(watch.fd, None)
        kevent = select.kevent(
            watch.fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_DELETE,
        )
        try:
            self._kq.control([kevent], 0, 0)
        except Exception:
            pass
        try:
            os.close(watch.fd)
        except OSError:
            pass

    async def _watch_loop(self) -> None:
        """Single asyncio task: blocks on kqueue in dedicated thread, fires callbacks."""
        loop = asyncio.get_running_loop()
        try:
            while self._running:
                try:
                    events = await loop.run_in_executor(
                        _KQUEUE_EXECUTOR, self._kq.control, None, 32, 0.5
                    )
                except Exception:
                    if self._running:
                        logger.debug("kqueue error in watch loop", exc_info=True)
                    break

                for event in events:
                    fd = event.ident
                    with self._lock:
                        watch = self._fd_to_watch.get(fd)
                    if watch is None:
                        continue

                    if watch.pipe_path is not None:
                        try:
                            os.truncate(watch.pipe_path, 0)
                        except OSError:
                            pass

                    try:
                        if watch.callback_arg is not None:
                            watch.callback(watch.callback_arg)
                        else:
                            watch.callback()
                    except Exception:
                        logger.debug("kqueue callback error", exc_info=True)
        except asyncio.CancelledError:
            pass

    def _cleanup_stale_pipe_dirs(self) -> None:
        """Remove leftover sw-pipes-* dirs from previous runs."""
        import shutil
        tmp = self._pipe_dir.parent
        try:
            for d in tmp.iterdir():
                if d.is_dir() and d.name.startswith("sw-pipes-") \
                        and d not in PaneWatcher._active_pipe_dirs:
                    try:
                        shutil.rmtree(d, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass
