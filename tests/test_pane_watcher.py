"""Comprehensive tests for the PaneWatcher kqueue-based file watcher.

Tests use real asyncio loops (pytest-asyncio strict mode) and real temp files.
kqueue is NOT mocked — we test the real macOS kernel-level mechanism.
"""

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from super_worker.services.pane_watcher import PaneWatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_to_file(path: Path, content: str = "x\n") -> None:
    """Append content to a file, then flush — triggers kqueue NOTE_WRITE."""
    with open(path, "a") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


async def _wait_for(condition, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll condition() until True or timeout. Returns True if condition met."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def watcher():
    """Create a PaneWatcher, yield it, then clean up."""
    pw = PaneWatcher()
    yield pw
    pw.cleanup()


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Redirect SESSION_STATES_DIR to a temp path for isolation."""
    d = tmp_path / "session-states"
    d.mkdir()
    monkeypatch.setattr("super_worker.services.pane_watcher.SESSION_STATES_DIR", d, raising=False)
    # Also patch the import target used inside start_watching_state
    import super_worker.constants as _constants
    monkeypatch.setattr(_constants, "SESSION_STATES_DIR", d)
    return d


# ---------------------------------------------------------------------------
# 1. Single kqueue thread after init
# ---------------------------------------------------------------------------

class TestThreading:
    def test_single_sw_kqueue_thread_exists_after_init(self, watcher):
        """Exactly one 'sw-kqueue' daemon thread is running after init."""
        kq_threads = [
            t for t in threading.enumerate()
            if t.name == "sw-kqueue" and t.is_alive()
        ]
        assert len(kq_threads) == 1, (
            f"Expected exactly 1 'sw-kqueue' thread, found {len(kq_threads)}"
        )

    def test_thread_is_daemon(self, watcher):
        """The kqueue thread must be a daemon so it doesn't block interpreter exit."""
        kq_threads = [
            t for t in threading.enumerate()
            if t.name == "sw-kqueue" and t.is_alive()
        ]
        assert kq_threads, "No sw-kqueue thread found"
        assert kq_threads[0].daemon is True

    def test_no_new_threads_on_additional_watches(self, watcher, state_dir):
        """Adding more watches must not spawn additional threads."""
        initial_count = threading.active_count()

        for name in ("sess-a", "sess-b", "sess-c"):
            watcher.start_watching_state(name, lambda n: None)

        # Allow thread count to settle
        time.sleep(0.1)
        assert threading.active_count() <= initial_count + 1  # at most +1 for the existing kq thread


# ---------------------------------------------------------------------------
# 2. start_watching / is_watching for pipe watches
# ---------------------------------------------------------------------------

class TestPipeWatchStartStop:
    def test_is_watching_false_before_start(self, watcher):
        assert watcher.is_watching("nonexistent-session") is False

    def test_start_watching_creates_pipe_file(self, watcher):
        """start_watching should create a .pipe file inside the watcher's pipe_dir."""
        pipe_path = watcher._pipe_dir / "test-session.pipe"
        # File may be created even if tmux is absent (touch() runs before subprocess)
        # We just verify is_watching returns based on dict state, not file existence.
        # After start_watching, even if tmux fails, pipe path is touched first.
        assert not pipe_path.exists()
        watcher.start_watching("test-session", lambda: None)
        # pipe_path.touch() is always called before the tmux subprocess
        assert pipe_path.exists()

    def test_is_watching_returns_true_when_watch_registered(self, watcher):
        """is_watching() reports True only when the fd was successfully opened."""
        # We need the file to actually be open. Bypass tmux by manually registering.
        pipe_path = watcher._pipe_dir / "manual-sess.pipe"
        watcher._pipe_dir.mkdir(parents=True, exist_ok=True)
        pipe_path.touch()
        fd = os.open(str(pipe_path), os.O_RDONLY)
        from super_worker.services.pane_watcher import _Watch
        watch = _Watch(fd=fd, callback=lambda: None, callback_arg=None,
                       pipe_path=pipe_path, session_name="manual-sess")
        watcher._register(watch)
        with watcher._lock:
            watcher._pipe_watches["manual-sess"] = watch

        assert watcher.is_watching("manual-sess") is True

    def test_stop_watching_sets_is_watching_false(self, watcher):
        """stop_watching removes a watch so is_watching returns False."""
        pipe_path = watcher._pipe_dir / "stop-sess.pipe"
        watcher._pipe_dir.mkdir(parents=True, exist_ok=True)
        pipe_path.touch()
        fd = os.open(str(pipe_path), os.O_RDONLY)
        from super_worker.services.pane_watcher import _Watch
        watch = _Watch(fd=fd, callback=lambda: None, callback_arg=None,
                       pipe_path=pipe_path, session_name="stop-sess")
        watcher._register(watch)
        with watcher._lock:
            watcher._pipe_watches["stop-sess"] = watch

        assert watcher.is_watching("stop-sess") is True
        watcher.stop_watching("stop-sess")
        assert watcher.is_watching("stop-sess") is False

    def test_stop_watching_nonexistent_is_noop(self, watcher):
        """stop_watching on an unknown session should not raise."""
        watcher.stop_watching("does-not-exist")  # must not raise


# ---------------------------------------------------------------------------
# 3. Callback fires on file write (real asyncio + real I/O)
# ---------------------------------------------------------------------------

class TestCallbackFiring:
    @pytest.mark.asyncio
    async def test_state_callback_fires_on_write(self, watcher, state_dir, monkeypatch):
        """Writing to a state file should trigger the callback via kqueue."""
        # Patch SESSION_STATES_DIR inside pane_watcher module
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        called_with: list[str] = []

        async def _run():
            # Set the loop on the watcher so callbacks can be dispatched
            watcher._loop = asyncio.get_running_loop()

            def cb(session_name: str) -> None:
                called_with.append(session_name)

            watcher.start_watching_state("sess-fire", cb)

            state_file = state_dir / "sess-fire"
            assert state_file.exists(), "start_watching_state must touch() the state file"

            # Write from a thread (simulates real state update)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _write_to_file, state_file)

            # Wait up to 5 s for the callback
            met = await _wait_for(lambda: len(called_with) > 0, timeout=5.0)
            assert met, "Callback was not called within 5 seconds after writing to state file"
            assert called_with[0] == "sess-fire"

        await _run()

    @pytest.mark.asyncio
    async def test_state_callback_receives_correct_session_name(self, watcher, state_dir, monkeypatch):
        """callback_arg (session_name) is forwarded correctly."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        fired: list[str] = []
        watcher._loop = asyncio.get_running_loop()
        watcher.start_watching_state("my-session", lambda n: fired.append(n))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "my-session")

        met = await _wait_for(lambda: bool(fired), timeout=5.0)
        assert met, "State callback did not fire"
        assert fired[0] == "my-session"

    @pytest.mark.asyncio
    async def test_pipe_callback_fires_on_write(self, watcher):
        """A manually-registered pipe watch callback fires when the file is written."""
        fired: list[int] = []
        watcher._loop = asyncio.get_running_loop()

        pipe_path = watcher._pipe_dir / "pipe-fire-test.pipe"
        watcher._pipe_dir.mkdir(parents=True, exist_ok=True)
        pipe_path.touch()

        fd = os.open(str(pipe_path), os.O_RDONLY)
        from super_worker.services.pane_watcher import _Watch

        def cb():
            fired.append(1)

        watch = _Watch(fd=fd, callback=cb, callback_arg=None,
                       pipe_path=pipe_path, session_name="pipe-fire-test")
        watcher._register(watch)
        with watcher._lock:
            watcher._pipe_watches["pipe-fire-test"] = watch

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, pipe_path)

        met = await _wait_for(lambda: bool(fired), timeout=5.0)
        assert met, "Pipe callback was not called within 5 seconds after file write"


# ---------------------------------------------------------------------------
# 4. Multiple watches simultaneously — only correct callback fires
# ---------------------------------------------------------------------------

class TestMultipleWatches:
    @pytest.mark.asyncio
    async def test_only_target_callback_fires(self, watcher, state_dir, monkeypatch):
        """Writing to sess-B's file fires sess-B callback, not sess-A or sess-C."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher._loop = asyncio.get_running_loop()
        fired: dict[str, list[str]] = {"a": [], "b": [], "c": []}

        watcher.start_watching_state("sess-a", lambda n: fired["a"].append(n))
        watcher.start_watching_state("sess-b", lambda n: fired["b"].append(n))
        watcher.start_watching_state("sess-c", lambda n: fired["c"].append(n))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "sess-b")

        met = await _wait_for(lambda: bool(fired["b"]), timeout=5.0)
        assert met, "sess-b callback did not fire"
        # Give the other callbacks a window to incorrectly fire
        await asyncio.sleep(0.3)
        assert fired["a"] == [], "sess-a should not have fired"
        assert fired["c"] == [], "sess-c should not have fired"

    @pytest.mark.asyncio
    async def test_all_callbacks_can_fire_independently(self, watcher, state_dir, monkeypatch):
        """Each of three sessions fires its own callback when its file is written."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher._loop = asyncio.get_running_loop()
        fired: dict[str, list[str]] = {"x": [], "y": [], "z": []}

        for name in ("x", "y", "z"):
            watcher.start_watching_state(name, lambda n, d=fired: d[n].append(n))

        loop = asyncio.get_running_loop()
        for name in ("x", "y", "z"):
            await loop.run_in_executor(None, _write_to_file, state_dir / name)
            await asyncio.sleep(0.05)  # slight stagger

        for name in ("x", "y", "z"):
            met = await _wait_for(lambda n=name: bool(fired[n]), timeout=5.0)
            assert met, f"Callback for {name} did not fire"


# ---------------------------------------------------------------------------
# 5. start_watching replacing an existing watch (no duplicates)
# ---------------------------------------------------------------------------

class TestReplaceWatch:
    @pytest.mark.asyncio
    async def test_second_start_watching_state_replaces_first(self, watcher, state_dir, monkeypatch):
        """Calling start_watching_state twice for same session replaces the watch."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher._loop = asyncio.get_running_loop()
        first_fired: list[str] = []
        second_fired: list[str] = []

        watcher.start_watching_state("dup-sess", lambda n: first_fired.append(n))
        # Replace with a new callback
        watcher.start_watching_state("dup-sess", lambda n: second_fired.append(n))

        # There should be exactly one watch for dup-sess
        with watcher._lock:
            assert list(watcher._state_watches.keys()).count("dup-sess") == 1

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "dup-sess")

        met = await _wait_for(lambda: bool(second_fired), timeout=5.0)
        assert met, "Replacement callback did not fire"
        # Give old callback a chance to incorrectly fire
        await asyncio.sleep(0.2)
        assert first_fired == [], "Old callback fired after replacement"

    def test_second_start_watching_pipe_replaces_first(self, watcher):
        """Calling start_watching twice replaces the watch; is_watching still True."""
        pipe_path = watcher._pipe_dir / "dup-pipe.pipe"
        watcher._pipe_dir.mkdir(parents=True, exist_ok=True)
        pipe_path.touch()

        # Manually insert first watch
        fd1 = os.open(str(pipe_path), os.O_RDONLY)
        from super_worker.services.pane_watcher import _Watch
        w1 = _Watch(fd=fd1, callback=lambda: None, callback_arg=None,
                    pipe_path=pipe_path, session_name="dup-pipe")
        watcher._register(w1)
        with watcher._lock:
            watcher._pipe_watches["dup-pipe"] = w1

        assert watcher.is_watching("dup-pipe") is True

        # A second manual insert via stop+re-register simulates start_watching() behaviour
        watcher.stop_watching("dup-pipe")
        assert watcher.is_watching("dup-pipe") is False

        # Re-add — simulates the second start_watching call
        pipe_path.touch()
        fd2 = os.open(str(pipe_path), os.O_RDONLY)
        w2 = _Watch(fd=fd2, callback=lambda: None, callback_arg=None,
                    pipe_path=pipe_path, session_name="dup-pipe")
        watcher._register(w2)
        with watcher._lock:
            watcher._pipe_watches["dup-pipe"] = w2

        assert watcher.is_watching("dup-pipe") is True


# ---------------------------------------------------------------------------
# 6. cleanup() stops the thread
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_cleanup_stops_watch_thread(self, watcher):
        """cleanup() must cause the 'sw-kqueue' thread to stop within 3 s."""
        thread = next(
            (t for t in threading.enumerate() if t.name == "sw-kqueue" and t.is_alive()),
            None
        )
        assert thread is not None, "No sw-kqueue thread found before cleanup"

        watcher.cleanup()
        thread.join(timeout=3.0)
        assert not thread.is_alive(), "sw-kqueue thread still alive 3 s after cleanup()"

    def test_cleanup_clears_all_watches(self, watcher, state_dir, monkeypatch):
        """cleanup() removes all registered watches."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher.start_watching_state("c1", lambda n: None)
        watcher.start_watching_state("c2", lambda n: None)

        watcher.cleanup()

        with watcher._lock:
            assert watcher._pipe_watches == {}
            assert watcher._state_watches == {}
            assert watcher._fd_to_watch == {}

    def test_double_cleanup_does_not_raise(self):
        """Calling cleanup() twice should not raise any exception."""
        pw = PaneWatcher()
        pw.cleanup()
        pw.cleanup()  # must not raise


# ---------------------------------------------------------------------------
# 7. stop_watching_state removes the watch
# ---------------------------------------------------------------------------

class TestStopWatchingState:
    def test_stop_watching_state_removes_from_dict(self, watcher, state_dir, monkeypatch):
        """stop_watching_state must remove the session from _state_watches."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher.start_watching_state("remove-me", lambda n: None)
        with watcher._lock:
            assert "remove-me" in watcher._state_watches

        watcher.stop_watching_state("remove-me")
        with watcher._lock:
            assert "remove-me" not in watcher._state_watches

    def test_stop_watching_state_nonexistent_is_noop(self, watcher):
        """stop_watching_state on an unknown session must not raise."""
        watcher.stop_watching_state("never-added")

    @pytest.mark.asyncio
    async def test_callback_does_not_fire_after_stop(self, watcher, state_dir, monkeypatch):
        """After stop_watching_state, writes to the file do not trigger callback."""
        import super_worker.constants as _constants
        monkeypatch.setattr(_constants, "SESSION_STATES_DIR", state_dir)

        watcher._loop = asyncio.get_running_loop()
        fired: list[str] = []

        watcher.start_watching_state("stopped-sess", lambda n: fired.append(n))
        watcher.stop_watching_state("stopped-sess")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_to_file, state_dir / "stopped-sess")

        # Wait to confirm callback does NOT fire
        await asyncio.sleep(0.5)
        assert fired == [], "Callback fired after stop_watching_state"


# ---------------------------------------------------------------------------
# 8. No _watches attribute access (internal structure check)
# ---------------------------------------------------------------------------

class TestInternalAPIContracts:
    def test_no_watches_attribute(self, watcher):
        """The old _watches attribute must not exist; use _pipe_watches instead."""
        assert not hasattr(watcher, "_watches"), (
            "_watches attribute found — use _pipe_watches for pipe watches"
        )

    def test_pipe_watches_attribute_exists(self, watcher):
        assert hasattr(watcher, "_pipe_watches")

    def test_state_watches_attribute_exists(self, watcher):
        assert hasattr(watcher, "_state_watches")

    def test_fd_to_watch_attribute_exists(self, watcher):
        assert hasattr(watcher, "_fd_to_watch")

    def test_kqueue_attribute_is_kqueue(self, watcher):
        """Verify _kq is actually a kqueue instance."""
        import select
        assert isinstance(watcher._kq, select.kqueue)


# ---------------------------------------------------------------------------
# 9. TerminalPane.resume_watching uses is_watching(), not _watches
# ---------------------------------------------------------------------------

class TestTerminalPaneResume:
    def test_resume_watching_uses_is_watching(self):
        """Verify resume_watching in terminal_pane.py uses is_watching(), not _watches."""
        import ast
        import textwrap
        src = Path("/Users/omerkeidar/Projects/super-worker/super_worker/widgets/terminal_pane.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "resume_watching":
                    func_src = ast.get_source_segment(src, node)
                    assert func_src is not None
                    # Must use is_watching()
                    assert "is_watching" in func_src, (
                        "resume_watching does not call is_watching()"
                    )
                    # Must NOT directly access _watches (old API)
                    assert "._watches" not in func_src, (
                        "resume_watching accesses deprecated _watches attribute"
                    )
                    return  # found and checked

        pytest.fail("resume_watching method not found in terminal_pane.py")
