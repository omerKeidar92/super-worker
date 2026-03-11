"""Watch tmux pane output and session state files via kqueue for efficient updates."""

import logging
import os
import select
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class _Watch:
    """A single watched fd with its dispatch callback."""
    fd: int
    callback: Callable        # called as callback() or callback(session_name)
    callback_arg: str | None  # None for pipe watches, session_name for state watches
    # Extra cleanup for pipe watches
    pipe_path: Path | None = None
    session_name: str | None = None


class PaneWatcher:
    """Watches tmux pane output and state files via a single shared kqueue.

    One background thread blocks on kq.control() waiting for events from all
    watched fds simultaneously. On macOS kqueue supports watching many fds in
    one call, so this uses exactly one thread regardless of session count —
    no thread pool exhaustion, no per-session threads.
    """

    # Track all active pipe dirs across instances so cleanup doesn't nuke siblings
    _active_pipe_dirs: set[Path] = set()

    def __init__(self) -> None:
        self._kq = select.kqueue()
        self._lock = threading.Lock()
        # fd -> _Watch for dispatch
        self._fd_to_watch: dict[int, _Watch] = {}
        # session_name -> _Watch for pipe watches
        self._pipe_watches: dict[str, _Watch] = {}
        # session_name -> _Watch for state file watches
        self._state_watches: dict[str, _Watch] = {}

        self._pipe_dir = Path(tempfile.mkdtemp(prefix="sw-pipes-"))
        PaneWatcher._active_pipe_dirs.add(self._pipe_dir)

        self._running = True
        self._loop = None  # set on first watch call

        self._thread = threading.Thread(
            target=self._watch_loop,
            name="sw-kqueue",
            daemon=True,
        )
        self._thread.start()
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

        watch = _Watch(
            fd=fd,
            callback=callback,
            callback_arg=None,
            pipe_path=pipe_path,
            session_name=session_name,
        )
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
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

        if watch.pipe_path:
            try:
                watch.pipe_path.unlink(missing_ok=True)
            except OSError:
                pass

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

    def is_watching(self, session_name: str) -> bool:
        """Return True if pipe-pane output is currently being watched."""
        with self._lock:
            return session_name in self._pipe_watches

    def stop_watching_state(self, session_name: str) -> None:
        """Stop watching a session's state file."""
        with self._lock:
            watch = self._state_watches.pop(session_name, None)
        if watch is None:
            return
        self._unregister(watch)

    def cleanup(self) -> None:
        """Stop all watches and shut down the watcher thread."""
        self._running = False
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
        """Add a fd to the shared kqueue and fd dispatch table."""
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

        import asyncio
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _unregister(self, watch: _Watch) -> None:
        """Remove a fd from the shared kqueue and close it."""
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

    def _watch_loop(self) -> None:
        """Single background thread: blocks on kqueue, dispatches all events."""
        while self._running:
            try:
                events = self._kq.control(None, 32, 0.5)
            except Exception:
                if self._running:
                    logger.debug("kqueue.control error in watch loop", exc_info=True)
                break

            for event in events:
                fd = event.ident
                with self._lock:
                    watch = self._fd_to_watch.get(fd)
                if watch is None:
                    continue

                # Truncate pipe files (notification channel only, never read)
                if watch.pipe_path is not None:
                    try:
                        os.truncate(watch.pipe_path, 0)
                    except OSError:
                        pass

                # Dispatch callback on the asyncio event loop (thread-safe)
                loop = self._loop
                if loop is None or not loop.is_running():
                    continue
                try:
                    if watch.callback_arg is not None:
                        loop.call_soon_threadsafe(watch.callback, watch.callback_arg)
                    else:
                        loop.call_soon_threadsafe(watch.callback)
                except Exception:
                    logger.debug("Failed to dispatch kqueue callback", exc_info=True)

    def _cleanup_stale_pipe_dirs(self) -> None:
        """Remove leftover sw-pipes-* dirs from previous runs."""
        import shutil
        tmp = self._pipe_dir.parent
        try:
            for d in tmp.iterdir():
                if d.is_dir() and d.name.startswith("sw-pipes-") and d not in PaneWatcher._active_pipe_dirs:
                    try:
                        shutil.rmtree(d, ignore_errors=True)
                    except Exception:
                        pass
        except Exception:
            pass
