"""Watch tmux pane output via pipe-pane and kqueue for efficient terminal updates."""

import asyncio
import logging
import os
import select
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class _PaneWatch:
    """State for a single pane being watched."""

    session_name: str
    pipe_path: Path
    callback: Callable
    task: asyncio.Task | None = None
    fd: int = -1


class PaneWatcher:
    """Watches tmux pane output via pipe-pane and notifies on changes.

    Uses tmux pipe-pane to stream output to a file, then monitors the file
    with kqueue (macOS) for changes. Only triggers capture-pane when there's
    actual new content, eliminating polling overhead for idle sessions.
    """

    # Track all active pipe dirs across instances so cleanup doesn't nuke siblings
    _active_pipe_dirs: set[Path] = set()

    def __init__(self) -> None:
        self._watches: dict[str, _PaneWatch] = {}
        self._pipe_dir = Path(tempfile.mkdtemp(prefix="sw-pipes-"))
        PaneWatcher._active_pipe_dirs.add(self._pipe_dir)
        self._running = True
        self._cleanup_stale_pipe_dirs()

    def start_watching(self, session_name: str, callback: Callable) -> None:
        """Start pipe-pane for a session. Calls callback when output arrives."""
        if session_name in self._watches:
            self.stop_watching(session_name)

        pipe_path = self._pipe_dir / f"{session_name}.pipe"
        # Ensure pipe dir still exists (may have been cleaned by another instance)
        self._pipe_dir.mkdir(parents=True, exist_ok=True)
        # Create the file so kqueue can watch it
        pipe_path.touch()

        # Start tmux pipe-pane to append output to our file
        try:
            subprocess.run(
                ["tmux", "pipe-pane", "-t", session_name, "-o", f"cat >> {pipe_path}"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            logger.debug("Failed to start pipe-pane for %s", session_name)
            return

        watch = _PaneWatch(
            session_name=session_name,
            pipe_path=pipe_path,
            callback=callback,
        )
        self._watches[session_name] = watch

        # Start async file watcher
        try:
            loop = asyncio.get_running_loop()
            watch.task = loop.create_task(self._watch_file(watch))
        except RuntimeError:
            # No running loop — caller will need to handle fallback polling
            logger.debug("No event loop available for file watching")

    def stop_watching(self, session_name: str) -> None:
        """Stop pipe-pane and file watching for a session."""
        watch = self._watches.pop(session_name, None)
        if watch is None:
            return

        # Cancel the watcher task
        if watch.task is not None:
            watch.task.cancel()

        # Close the file descriptor
        if watch.fd >= 0:
            try:
                os.close(watch.fd)
            except OSError:
                pass

        # Disable pipe-pane
        try:
            subprocess.run(
                ["tmux", "pipe-pane", "-t", session_name],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

        # Clean up pipe file
        try:
            watch.pipe_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def _watch_file(self, watch: _PaneWatch) -> None:
        """Watch a file for modifications using kqueue."""
        try:
            fd = os.open(str(watch.pipe_path), os.O_RDONLY)
            watch.fd = fd
        except OSError:
            logger.debug("Failed to open pipe file for watching: %s", watch.pipe_path)
            return

        try:
            kq = select.kqueue()
            event = select.kevent(
                fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE,
            )
            kq.control([event], 0, 0)

            while self._running:
                # Check for changes — run in thread to avoid blocking the event loop
                events = await asyncio.to_thread(kq.control, None, 1, 0.5)
                if events:
                    # Debounce: wait for burst writes to settle
                    await asyncio.sleep(0.016)
                    try:
                        watch.callback()
                    except Exception:
                        logger.debug("Pane watcher callback error", exc_info=True)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("File watcher error for %s", watch.session_name, exc_info=True)
        finally:
            try:
                kq.close()
            except Exception:
                pass

    def cleanup(self) -> None:
        """Stop all watches, remove temp dir."""
        self._running = False
        for name in list(self._watches):
            self.stop_watching(name)
        PaneWatcher._active_pipe_dirs.discard(self._pipe_dir)
        import shutil
        try:
            shutil.rmtree(self._pipe_dir, ignore_errors=True)
        except Exception:
            pass

    def _cleanup_stale_pipe_dirs(self) -> None:
        """Remove leftover sw-pipes-* dirs from previous runs."""
        import shutil
        tmp = self._pipe_dir.parent
        for d in tmp.iterdir():
            if d.is_dir() and d.name.startswith("sw-pipes-") and d not in PaneWatcher._active_pipe_dirs:
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
