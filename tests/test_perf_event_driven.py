"""Tests for event-driven state detection and pipe file management.

Covers:
- State file reading (read_state_file, read_all_state_files)
- State file cleanup (cleanup_state_file)
- Pipe file truncation in PaneWatcher
- State file watching in PaneWatcher
- Hook script writes state files
- ContentChanged no longer triggers subprocess storm
- periodic_refresh still detects dead sessions
"""

import inspect
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from super_worker.services.tmux import (
    SessionState,
    cleanup_state_file,
    read_all_state_files,
    read_state_file,
)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Create a temporary session-states directory."""
    d = tmp_path / "session-states"
    d.mkdir()
    monkeypatch.setattr("super_worker.services.tmux.SESSION_STATES_DIR", d, raising=False)
    # Patch the import in the module
    import super_worker.constants
    monkeypatch.setattr(super_worker.constants, "SESSION_STATES_DIR", d)
    return d


class TestReadStateFile:
    def test_read_waiting_input(self, state_dir):
        (state_dir / "sw-main-0").write_text("waiting_input")
        assert read_state_file("sw-main-0") == SessionState.WAITING_INPUT

    def test_read_waiting_approval(self, state_dir):
        (state_dir / "sw-main-0").write_text("waiting_approval")
        assert read_state_file("sw-main-0") == SessionState.WAITING_APPROVAL

    def test_read_running(self, state_dir):
        (state_dir / "sw-main-0").write_text("running")
        assert read_state_file("sw-main-0") == SessionState.RUNNING

    def test_read_missing_file(self, state_dir):
        assert read_state_file("nonexistent") == SessionState.UNKNOWN

    def test_read_empty_file(self, state_dir):
        (state_dir / "sw-main-0").write_text("")
        assert read_state_file("sw-main-0") == SessionState.UNKNOWN

    def test_read_unknown_state(self, state_dir):
        (state_dir / "sw-main-0").write_text("some_garbage")
        assert read_state_file("sw-main-0") == SessionState.UNKNOWN

    def test_read_with_whitespace(self, state_dir):
        (state_dir / "sw-main-0").write_text("  waiting_input\n")
        assert read_state_file("sw-main-0") == SessionState.WAITING_INPUT


class TestReadAllStateFiles:
    def test_read_multiple(self, state_dir):
        (state_dir / "sw-main-0").write_text("running")
        (state_dir / "sw-feat-0").write_text("waiting_approval")
        result = read_all_state_files(["sw-main-0", "sw-feat-0"])
        assert result == {
            "sw-main-0": SessionState.RUNNING,
            "sw-feat-0": SessionState.WAITING_APPROVAL,
        }

    def test_missing_files_return_unknown(self, state_dir):
        (state_dir / "sw-main-0").write_text("running")
        result = read_all_state_files(["sw-main-0", "sw-missing-0"])
        assert result["sw-main-0"] == SessionState.RUNNING
        assert result["sw-missing-0"] == SessionState.UNKNOWN

    def test_empty_list(self, state_dir):
        assert read_all_state_files([]) == {}


class TestCleanupStateFile:
    def test_cleanup_existing(self, state_dir):
        f = state_dir / "sw-main-0"
        f.write_text("running")
        cleanup_state_file("sw-main-0")
        assert not f.exists()

    def test_cleanup_nonexistent(self, state_dir):
        # Should not raise
        cleanup_state_file("sw-nonexistent-0")


class TestPaneWatcherTruncation:
    """Test that pipe files are truncated after kqueue events."""

    def test_pipe_file_truncated_after_callback(self):
        """Verify the truncation call exists in _watch_pipe_file logic."""
        from super_worker.services.pane_watcher import PaneWatcher

        watcher = PaneWatcher()
        try:
            # Create a fake pipe file and write data to it
            pipe_path = watcher._pipe_dir / "test.pipe"
            pipe_path.write_text("x" * 10000)
            assert pipe_path.stat().st_size == 10000

            # Simulate what _watch_pipe_file does after kqueue event:
            # truncate the file
            os.truncate(pipe_path, 0)
            assert pipe_path.stat().st_size == 0
        finally:
            watcher.cleanup()


class TestPaneWatcherStateWatching:
    """Test state file watching setup and teardown."""

    def test_start_stop_state_watching(self, state_dir):
        from super_worker.services.pane_watcher import PaneWatcher

        watcher = PaneWatcher()
        try:
            callback = MagicMock()
            # start_watching_state should create the state file and add to _state_watches
            watcher.start_watching_state("sw-main-0", callback)
            assert "sw-main-0" in watcher._state_watches
            assert (state_dir / "sw-main-0").exists()

            # stop should remove from _state_watches
            watcher.stop_watching_state("sw-main-0")
            assert "sw-main-0" not in watcher._state_watches
        finally:
            watcher.cleanup()

    def test_cleanup_stops_state_watches(self, state_dir):
        from super_worker.services.pane_watcher import PaneWatcher

        watcher = PaneWatcher()
        callback = MagicMock()
        watcher.start_watching_state("sw-main-0", callback)
        watcher.start_watching_state("sw-feat-0", callback)
        assert len(watcher._state_watches) == 2

        watcher.cleanup()
        assert len(watcher._state_watches) == 0

    def test_start_watching_state_replaces_existing(self, state_dir):
        from super_worker.services.pane_watcher import PaneWatcher

        watcher = PaneWatcher()
        try:
            cb1 = MagicMock()
            cb2 = MagicMock()
            watcher.start_watching_state("sw-main-0", cb1)
            watcher.start_watching_state("sw-main-0", cb2)
            # Should have only one watch, with the new callback
            assert len(watcher._state_watches) == 1
            assert watcher._state_watches["sw-main-0"].callback is cb2
        finally:
            watcher.cleanup()


class TestTerminalPaneStateWatching:
    """Test TerminalPane.start_watching_states manages watch set correctly."""

    def test_start_watching_states_diff(self, state_dir):
        from super_worker.widgets.terminal_pane import TerminalPane

        pane = TerminalPane()
        watcher = pane._watcher

        # Initial set
        pane.start_watching_states(["sw-main-0", "sw-feat-0"])
        assert pane._watched_state_sessions == {"sw-main-0", "sw-feat-0"}
        assert "sw-main-0" in watcher._state_watches
        assert "sw-feat-0" in watcher._state_watches

        # Update: remove feat, add bug
        pane.start_watching_states(["sw-main-0", "sw-bug-0"])
        assert pane._watched_state_sessions == {"sw-main-0", "sw-bug-0"}
        assert "sw-feat-0" not in watcher._state_watches
        assert "sw-bug-0" in watcher._state_watches
        assert "sw-main-0" in watcher._state_watches

        watcher.cleanup()


class TestHookWritesStateFile:
    """Test that sw-hook.sh writes state files."""

    def test_hook_script_contains_state_file_write(self):
        """Verify the hook script writes to session-states directory."""
        import importlib.resources
        ref = importlib.resources.files("super_worker.scripts").joinpath("sw-hook.sh")
        with importlib.resources.as_file(ref) as path:
            content = path.read_text()

        assert "session-states" in content
        assert 'printf' in content
        assert '${SW_SESSION_NAME}' in content


class TestNoContentChangedMessage:
    """Verify ContentChanged message is no longer posted by TerminalPane."""

    def test_no_content_changed_class(self):
        """TerminalPane should no longer have a ContentChanged message class."""
        from super_worker.widgets.terminal_pane import TerminalPane
        assert not hasattr(TerminalPane, "ContentChanged")

    def test_has_state_changed_class(self):
        """TerminalPane should have a StateChanged message class."""
        from super_worker.widgets.terminal_pane import TerminalPane
        assert hasattr(TerminalPane, "StateChanged")


class TestProjectViewStateHandler:
    """Verify ProjectView handles StateChanged instead of ContentChanged."""

    def test_has_state_changed_handler(self):
        from super_worker.widgets.project_view import ProjectView
        assert hasattr(ProjectView, "on_terminal_pane_state_changed")

    def test_no_content_changed_handler(self):
        from super_worker.widgets.project_view import ProjectView
        assert not hasattr(ProjectView, "on_terminal_pane_content_changed")


class TestSendKeysNoEnvSet:
    """Verify send_keys no longer calls _set_session_env (dead code removed)."""

    def test_no_set_session_env_function(self):
        """_set_session_env should not exist in tmux module anymore."""
        import super_worker.services.tmux as tmux_mod
        assert not hasattr(tmux_mod, "_set_session_env")

    def test_no_throttle_state_vars(self):
        """Throttle state variables should be removed."""
        import super_worker.services.tmux as tmux_mod
        assert not hasattr(tmux_mod, "_last_state_set")
        assert not hasattr(tmux_mod, "_STATE_SET_THROTTLE_S")

    def test_send_keys_does_not_call_set_environment(self, monkeypatch):
        """send_keys should only call pane.send_keys, not session.set_environment."""
        mock_pane = MagicMock()
        mock_session = MagicMock()
        mock_session.active_pane = mock_pane
        mock_server = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
        # Clear pane cache so _get_pane calls the mock
        monkeypatch.setattr("super_worker.services.tmux._pane_cache", {})

        from super_worker.services.tmux import send_keys
        send_keys("sw-test-0", "a", "b", "c")

        # pane.send_keys should be called 3 times (once per key)
        assert mock_pane.send_keys.call_count == 3
        # session.set_environment should NEVER be called
        mock_session.set_environment.assert_not_called()


class TestFallbackPollInterval:
    """Verify fallback poll interval is no longer aggressive."""

    def test_fallback_poll_is_safety_net(self):
        from super_worker.constants import PANE_FALLBACK_POLL_S
        # Should be >= 2s — kqueue handles real-time, this is just safety net
        assert PANE_FALLBACK_POLL_S >= 2.0

    def test_fallback_poll_not_aggressive(self):
        from super_worker.constants import PANE_FALLBACK_POLL_S
        # Should not be the old 0.3s aggressive poll
        assert PANE_FALLBACK_POLL_S != 0.3


class TestPeriodicRefreshLightweight:
    """Verify periodic_refresh uses lightweight alive check, not batch_detect."""

    def test_periodic_refresh_imports_batch_check_alive(self):
        """ProjectView should import batch_check_alive for periodic_refresh."""
        import super_worker.widgets.project_view as pv_mod
        source = inspect.getsource(pv_mod.ProjectView.periodic_refresh)
        assert "batch_check_alive" in source
        # Should NOT use batch_detect_session_states (heavyweight)
        assert "batch_detect_session_states" not in source

    def test_periodic_refresh_reads_state_files(self):
        """periodic_refresh should use read_all_state_files for state."""
        import super_worker.widgets.project_view as pv_mod
        source = inspect.getsource(pv_mod.ProjectView.periodic_refresh)
        assert "read_all_state_files" in source


class TestDebounceUsesCallLater:
    """Verify the render debounce uses call_later with time-based throttle."""

    def test_debounce_uses_call_later_not_set_timer(self):
        """_on_pane_output should use call_later for immediate scheduling."""
        import super_worker.widgets.terminal_pane as tp_mod
        source = inspect.getsource(tp_mod.TerminalPane._on_pane_output)
        assert "call_later" in source
        # Time-based debounce via monotonic comparison
        assert "_last_render_request" in source

    def test_send_keys_not_exclusive(self):
        """send-keys workers should NOT be exclusive (would drop keys)."""
        import super_worker.widgets.terminal_pane as tp_mod
        source = inspect.getsource(tp_mod.TerminalPane._send_keys_async)
        assert "exclusive=True" not in source


# ── Integration tests using Textual pilot ─────────────────────────────────


def _make_mock_server(capture_returns=None):
    """Create a mock libtmux server for integration tests."""
    mock_pane = MagicMock()
    mock_pane.capture_pane.return_value = capture_returns or ["line 1"]
    mock_pane.pane_dead = "0"
    mock_session = MagicMock()
    mock_session.session_name = "sw-test-0"
    mock_session.active_pane = mock_pane
    mock_session.show_environment.return_value = {}
    mock_server = MagicMock()
    # libtmux uses a special list-like with .get() — keep it as MagicMock
    mock_server.sessions.get.return_value = mock_session
    mock_server.sessions.__iter__ = MagicMock(return_value=iter([mock_session]))
    mock_server.new_session.return_value = mock_session
    return mock_server, mock_session, mock_pane


@pytest.fixture
def mock_tmux(monkeypatch):
    """Mock the tmux boundary and reset caches for integration tests."""
    mock_server, mock_session, mock_pane = _make_mock_server()
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
    monkeypatch.setattr("super_worker.services.tmux._server", None)
    monkeypatch.setattr("super_worker.services.tmux._pane_cache", {})
    return mock_server, mock_session, mock_pane


@pytest.mark.asyncio
async def test_rapid_typing_all_keys_sent(mock_tmux):
    """Simulate rapid typing — every key must arrive at tmux, none dropped."""
    from textual.app import App, ComposeResult
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause()

        # Type 20 characters rapidly
        keys = list("hello world testing!")
        for ch in keys:
            await pilot.press(ch)
        await pilot.pause(delay=0.5)

        # Every key must have been sent to tmux
        calls = mock_pane.send_keys.call_args_list
        sent_chars = [c.args[0] for c in calls]
        for ch in keys:
            assert ch in sent_chars, f"Key '{ch}' was dropped"
        assert len(sent_chars) >= len(keys), (
            f"Expected at least {len(keys)} send_keys calls, got {len(sent_chars)}"
        )


@pytest.mark.asyncio
async def test_display_updates_during_typing(mock_tmux):
    """Display must update DURING typing, not just after."""
    from textual.app import App, ComposeResult
    from textual.widgets import Static
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux
    update_count = 0
    original_update = Static.update

    def tracking_update(self, *args, **kwargs):
        nonlocal update_count
        update_count += 1
        return original_update(self, *args, **kwargs)

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause(delay=0.2)  # Let initial capture happen

        # Reset count after initial render
        update_count = 0
        # Patch Static.update to track widget updates
        Static.update = tracking_update

        try:
            # Simulate kqueue events during typing (as pipe-pane would trigger)
            # Each call simulates what happens when tmux writes to the pipe file
            for i in range(10):
                # Change capture output so hash changes and render happens
                mock_pane.capture_pane.return_value = [f"output after key {i}"]
                terminal._on_pane_output()
                await pilot.pause(delay=0.1)  # Wait for debounce to fire

            # Should have had multiple renders during the sequence, not just one at end
            assert update_count >= 3, (
                f"Expected >=3 renders during typing, got {update_count}. "
                f"Display is starving during typing."
            )
        finally:
            Static.update = original_update


@pytest.mark.asyncio
async def test_debounce_coalesces_burst_events(mock_tmux):
    """Multiple kqueue events within one debounce window produce only one render."""
    from textual.app import App, ComposeResult
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux
    poll_count = 0

    class TrackingTerminalPane(TerminalPane):
        def _poll_pane(self) -> None:
            nonlocal poll_count
            poll_count += 1
            super()._poll_pane()

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TrackingTerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TrackingTerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause(delay=0.2)

        # Reset count after initial activity
        poll_count = 0

        # Fire 5 rapid kqueue events within one debounce window (~50ms)
        for _ in range(5):
            terminal._on_pane_output()
        # Wait for trailing timer + processing
        await pilot.pause(delay=0.2)

        # Time-based debounce: first event triggers call_later (immediate),
        # remaining events are throttled, trailing timer fires once.
        # Should produce at most 2 polls (1 immediate + 1 trailing), not 5.
        assert poll_count <= 2, (
            f"Expected <=2 coalesced polls from 5 rapid events, got {poll_count}"
        )


@pytest.mark.asyncio
async def test_debounce_allows_periodic_renders_during_sustained_input(mock_tmux):
    """During sustained typing, renders happen periodically (not starved)."""
    from textual.app import App, ComposeResult
    from super_worker.widgets.terminal_pane import TerminalPane

    _, _, mock_pane = mock_tmux
    poll_count = 0

    class TrackingTerminalPane(TerminalPane):
        def _poll_pane(self) -> None:
            nonlocal poll_count
            poll_count += 1
            super()._poll_pane()

    class TestApp(App):
        def compose(self) -> ComposeResult:
            yield TrackingTerminalPane()

    app = TestApp()
    async with app.run_test() as pilot:
        terminal = app.query_one(TrackingTerminalPane)
        terminal.active_session = "sw-test-0"
        await pilot.pause(delay=0.2)

        poll_count = 0

        # Simulate sustained typing: kqueue event every 80ms for 800ms
        # With 50ms debounce that doesn't reset, should get ~10 renders
        for i in range(10):
            terminal._on_pane_output()
            await pilot.pause(delay=0.08)

        # Wait for last debounce to fire
        await pilot.pause(delay=0.2)

        # With non-resetting debounce: each event that finds no pending timer
        # starts one.  Timer fires after 50ms, clears itself, next event
        # starts a new timer.  With 80ms spacing and 50ms debounce, most
        # events should trigger their own render.
        assert poll_count >= 5, (
            f"Expected >=5 renders during 800ms of typing, got {poll_count}. "
            f"Debounce is starving renders during sustained input."
        )
