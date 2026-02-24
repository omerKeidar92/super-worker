"""Integration tests for tmux operations using real tmux sessions.

These tests create actual tmux sessions and verify behavior against real data,
not mocked responses.
"""

import libtmux
import pytest

from super_worker.services.tmux import (
    SessionState,
    batch_detect_session_states,
    capture_pane,
)


@pytest.fixture
def real_tmux_session():
    """Create a real tmux session for testing, clean up after."""
    server = libtmux.Server()
    session = server.new_session(session_name="test-integration-session")

    yield session

    try:
        session.kill()
    except Exception:
        pass


class TestBatchDetectSessionStatesIntegration:
    """Test batch_detect_session_states with real tmux sessions."""

    def test_detects_dead_session(self):
        """A session that doesn't exist should be marked DEAD."""
        result = batch_detect_session_states(["nonexistent-session-12345"])
        assert result["nonexistent-session-12345"] == SessionState.DEAD

    def test_detects_alive_session(self, real_tmux_session):
        """An alive session should be detected and state queried."""
        result = batch_detect_session_states([real_tmux_session.session_name])
        # Session exists but no SW_CC_STATE, so should be RUNNING
        assert result[real_tmux_session.session_name] == SessionState.RUNNING

    def test_detects_session_with_state(self, real_tmux_session):
        """A session with SW_CC_STATE env var should have its state detected."""
        # Set the tmux session environment variable using libtmux API
        real_tmux_session.set_environment("SW_CC_STATE", "waiting_input")

        result = batch_detect_session_states([real_tmux_session.session_name])
        assert result[real_tmux_session.session_name] == SessionState.WAITING_INPUT

    def test_mixed_dead_and_alive(self, real_tmux_session):
        """Mix of dead and alive sessions should be handled correctly."""
        result = batch_detect_session_states([
            real_tmux_session.session_name,
            "nonexistent-session-xyz"
        ])
        assert result[real_tmux_session.session_name] == SessionState.RUNNING
        assert result["nonexistent-session-xyz"] == SessionState.DEAD

class TestCapturePaneIntegration:
    """Test capture_pane with real tmux sessions."""

    def test_capture_existing_pane(self, real_tmux_session):
        """Capture pane should work with real session."""
        # Send some text to the pane so we can see it
        real_tmux_session.active_pane.send_keys("echo 'test content'", "Enter")

        result = capture_pane(real_tmux_session.session_name)
        # Should contain some output, not error message
        assert "not found" not in result.lower()
        # Should have content
        assert len(result) > 0

    def test_capture_nonexistent_pane(self):
        """Capture pane should handle nonexistent session gracefully."""
        result = capture_pane("nonexistent-session-xyz")
        # Should return a "not found" message, not crash
        assert "not found" in result.lower()
