from unittest.mock import MagicMock, call

import pytest

from super_worker.models import Session, Worktree
from super_worker.services.tmux import (
    SessionState,
    _find_available_session_name,
    batch_detect_session_states,
    capture_pane,
    create_session,
    is_session_alive,
    kill_session,
    kill_all_sessions,
    send_keys,
    tmux_session_name,
)


class TestTmuxSessionName:
    def test_format(self):
        name = tmux_session_name("my-feature", 0)
        assert name == "sw-my-feature-0"

    def test_incremented_index(self):
        name = tmux_session_name("feat", 3)
        assert name == "sw-feat-3"

def _mock_server(monkeypatch, session=None, pane=None):
    """Build a mock libtmux server with optional session and pane."""
    mock_pane = pane or MagicMock()
    mock_session = session or MagicMock()
    mock_session.active_pane = mock_pane
    mock_server = MagicMock()
    mock_server.sessions.get.return_value = mock_session
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
    return mock_server, mock_session, mock_pane


class TestCapturePane:
    def test_success(self, monkeypatch):
        _, _, mock_pane = _mock_server(monkeypatch)
        mock_pane.capture_pane.return_value = ["line1", "line2"]

        output = capture_pane("sw-feat-0")

        assert output == "line1\nline2"
        mock_pane.capture_pane.assert_called_once_with(start=-500, escape_sequences=True)

    def test_failure_returns_not_found(self, monkeypatch):
        mock_server = MagicMock()
        mock_server.sessions.get.side_effect = Exception("no session")
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        output = capture_pane("sw-dead-0")

        assert "not found" in output.lower()

    def test_empty_output(self, monkeypatch):
        _, _, mock_pane = _mock_server(monkeypatch)
        mock_pane.capture_pane.return_value = []

        output = capture_pane("sw-feat-0")

        assert output == ""


class TestSendKeys:
    def test_sends_key_to_pane(self, monkeypatch):
        _, _, mock_pane = _mock_server(monkeypatch)

        send_keys("sw-feat-0", "Enter")

        mock_pane.send_keys.assert_called_once_with("Enter", enter=False, literal=False)

    def test_literal_flag(self, monkeypatch):
        _, _, mock_pane = _mock_server(monkeypatch)

        send_keys("sw-feat-0", "hello", literal=True)

        mock_pane.send_keys.assert_called_once_with("hello", enter=False, literal=True)

    def test_multiple_keys_sent_in_order(self, monkeypatch):
        _, _, mock_pane = _mock_server(monkeypatch)

        send_keys("sw-feat-0", "y", "Enter")

        assert mock_pane.send_keys.call_count == 2
        mock_pane.send_keys.assert_has_calls([
            call("y", enter=False, literal=False),
            call("Enter", enter=False, literal=False),
        ])

    def test_dead_session_does_not_raise(self, monkeypatch):
        mock_server = MagicMock()
        mock_server.sessions.get.side_effect = Exception("no session")
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        send_keys("sw-dead-0", "Enter")  # Should not raise


class TestBatchDetectSessionStates:
    def _mock_alive(self, monkeypatch, sessions: list):
        mock_server = MagicMock()
        mock_server.sessions = sessions
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

    def test_empty_input(self):
        result = batch_detect_session_states([])
        assert result == {}

    def test_all_dead(self, monkeypatch):
        self._mock_alive(monkeypatch, [])

        result = batch_detect_session_states(["sw-a-0", "sw-b-0"])

        assert result["sw-a-0"] == SessionState.DEAD
        assert result["sw-b-0"] == SessionState.DEAD

    def test_mixed_alive_and_dead(self, monkeypatch):
        alive_session = MagicMock()
        alive_session.session_name = "sw-a-0"
        alive_session.show_environment.return_value = {"SW_CC_STATE": "waiting_input"}
        self._mock_alive(monkeypatch, [alive_session])

        result = batch_detect_session_states(["sw-a-0", "sw-b-0"])

        assert result["sw-a-0"] == SessionState.WAITING_INPUT
        assert result["sw-b-0"] == SessionState.DEAD

    @pytest.mark.parametrize("env,expected_state", [
        ({}, SessionState.RUNNING),
        ({"SW_CC_STATE": "running"}, SessionState.RUNNING),
        ({"SW_CC_STATE": "waiting_input"}, SessionState.WAITING_INPUT),
        ({"SW_CC_STATE": "waiting_approval"}, SessionState.WAITING_APPROVAL),
        ({"SW_CC_STATE": "unknown_value"}, SessionState.RUNNING),
    ])
    def test_alive_session_state(self, monkeypatch, env, expected_state):
        alive_session = MagicMock()
        alive_session.session_name = "sw-a-0"
        alive_session.show_environment.return_value = env
        self._mock_alive(monkeypatch, [alive_session])

        result = batch_detect_session_states(["sw-a-0"])

        assert result["sw-a-0"] == expected_state


class TestCreateSession:
    def _mock_server(self, monkeypatch, existing_sessions=None):
        mock_tmux_session = MagicMock()
        mock_server = MagicMock()
        mock_server.sessions = existing_sessions or []
        mock_server.new_session.return_value = mock_tmux_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)
        return mock_server

    def test_creates_session_with_defaults(self, monkeypatch):
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt)

        assert session.tmux_session_name.startswith("sw-feat-")
        assert session.label.startswith("session ")
        assert session.skip_permissions is False
        assert len(wt.sessions) == 0  # create_session no longer mutates worktree
        server.new_session.assert_called_once()

    def test_uses_prompt_as_label(self, monkeypatch):
        self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, prompt="/plan")

        assert session.label == "/plan"
        assert session.initial_prompt == "/plan"

    def test_explicit_label_overrides_prompt(self, monkeypatch):
        self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, prompt="/plan", label="My Label")

        assert session.label == "My Label"

    def test_skip_permissions_flag(self, monkeypatch):
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt, skip_permissions=True)

        assert session.skip_permissions is True
        cmd = server.new_session.call_args[1]["window_command"]
        assert "--dangerously-skip-permissions" in cmd

    def test_resume_flag(self, monkeypatch):
        server = self._mock_server(monkeypatch)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        create_session(wt, resume=True)

        cmd = server.new_session.call_args[1]["window_command"]
        assert "--continue" in cmd

    def test_avoids_name_collision(self, monkeypatch):
        existing = MagicMock()
        existing.session_name = "sw-feat-0"
        self._mock_server(monkeypatch, existing_sessions=[existing])
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")

        session = create_session(wt)

        assert session.tmux_session_name == "sw-feat-1"


@pytest.mark.parametrize("found,expected", [(True, True), (False, False)])
def test_is_session_alive(monkeypatch, found, expected):
    mock_server = MagicMock()
    if found:
        mock_server.sessions.get.return_value = MagicMock()
    else:
        mock_server.sessions.get.side_effect = Exception("not found")
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

    assert is_session_alive("sw-feat-0") is expected


class TestKillSession:
    def test_kills_existing_session(self, monkeypatch):
        mock_session = MagicMock()
        mock_server = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        kill_session("sw-feat-0")

        mock_session.kill.assert_called_once()

    def test_handles_missing_session(self, monkeypatch):
        mock_server = MagicMock()
        mock_server.sessions.get.side_effect = Exception("not found")
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        kill_session("sw-dead-0")  # Should not raise


class TestKillAllSessions:
    def test_kills_all_worktree_sessions(self, monkeypatch):
        killed = []

        def fake_kill(name):
            killed.append(name)

        monkeypatch.setattr("super_worker.services.tmux.kill_session", fake_kill)
        wt = Worktree(name="feat", path="/tmp/feat", branch="main")
        wt.sessions = [
            Session(tmux_session_name="sw-feat-0", label="s0"),
            Session(tmux_session_name="sw-feat-1", label="s1"),
        ]

        kill_all_sessions(wt)

        assert killed == ["sw-feat-0", "sw-feat-1"]
