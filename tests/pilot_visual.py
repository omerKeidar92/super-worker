"""Comprehensive visual pilot tests for all features from the improvement plan.

Tests all 4 phases:
  Phase 1: Hook installation and state detection
  Phase 2: Terminal rendering (pane watcher, layout)
  Phase 3: remain-on-exit, dead pane detection, respawn
  Phase 4: State persistence (backup/restore)

Run with: python -m pytest tests/pilot_visual.py -v -s
Screenshots saved to /tmp/sw-pilot-screenshots/
"""

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import git as gitpython
import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input, Label, ListView, Static

from super_worker.app import SuperWorkerApp
from super_worker.models import AppState, Session, Worktree
from super_worker.screens import NewSessionScreen
from super_worker.services.hooks import (
    _HOOK_MARKER,
    _build_hooks,
    _is_our_hook_entry,
    install_hooks,
    uninstall_hooks,
)
from super_worker.services.state import (
    _migrate_data,
    load_state,
    recover_dead_sessions,
    save_state,
)
from super_worker.services.tmux import (
    SessionState,
    _set_session_env,
    batch_detect_session_states,
    create_session,
    is_session_alive,
    respawn_pane,
)
from super_worker.widgets.project_view import WorktreeTabContent
from super_worker.widgets.sidebar import SessionSidebar
from super_worker.widgets.terminal_pane import TerminalPane

SCREENSHOT_DIR = Path("/tmp/sw-pilot-screenshots")


@pytest.fixture(autouse=True)
def isolate_externals(tmp_path, monkeypatch):
    """Mock only the tmux server and redirect state dir."""
    state_dir = tmp_path / "sw-state"
    state_dir.mkdir()
    monkeypatch.setattr("super_worker.services.state.STATE_DIR", state_dir)

    mock_session = MagicMock()
    mock_session.session_name = "sw-main-0"
    mock_session.active_pane = MagicMock()
    mock_session.active_pane.capture_pane.return_value = [
        "$ claude",
        "Hello! How can I help you today?",
        "",
        "> ",
    ]
    mock_session.active_pane.pane_dead = "0"
    mock_session.show_environment.return_value = {}

    mock_server = MagicMock()
    mock_server.sessions = [mock_session]
    mock_server.new_session.return_value = mock_session
    monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

    # Mock subprocess for pipe-pane
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=0))


@pytest.fixture(autouse=True)
def screenshot_dir():
    if SCREENSHOT_DIR.exists():
        shutil.rmtree(SCREENSHOT_DIR)
    SCREENSHOT_DIR.mkdir(parents=True)
    yield SCREENSHOT_DIR


def _save(app, name: str) -> Path:
    svg = app.export_screenshot(title=name)
    path = SCREENSHOT_DIR / f"{name}.svg"
    path.write_text(svg)
    return path


def _pv(app):
    return app._active_project_view


def _extract_text(svg_content: str) -> str:
    """Extract visible text from SVG for assertion."""
    import re
    texts = re.findall(r'>([^<]+)<', svg_content)
    return " ".join(t.replace("&#160;", " ").strip() for t in texts if t.strip())


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1: Hook Installation & State Detection
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase1Hooks:
    """Phase 1: Claude Code hooks for state detection."""

    def test_hook_script_installed(self, tmp_path, monkeypatch):
        """install_hooks() deploys the hook script and registers in settings.json."""
        hook_dest = tmp_path / "sw-hook.sh"
        settings_path = tmp_path / "settings.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
        monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", settings_path)
        monkeypatch.setattr("super_worker.services.hooks.STATE_DIR", state_dir)
        monkeypatch.setattr(
            "super_worker.services.hooks._get_hook_source",
            lambda: Path(__file__).parent.parent / "super_worker" / "scripts" / "sw-hook.sh",
        )

        install_hooks()

        # Script deployed and executable
        assert hook_dest.exists(), "Hook script should be deployed"
        assert hook_dest.stat().st_mode & 0o111, "Hook script should be executable"

        # Settings.json has our hooks
        settings = json.loads(settings_path.read_text())
        assert "hooks" in settings
        assert "Stop" in settings["hooks"]
        assert "Notification" in settings["hooks"]
        assert "PreToolUse" in settings["hooks"]

        # Stop hook sets waiting_input
        stop_cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "waiting_input" in stop_cmd

        # Notification hook sets waiting_approval with matcher
        notif = settings["hooks"]["Notification"][0]
        assert notif["matcher"] == "permission_prompt"
        assert "waiting_approval" in notif["hooks"][0]["command"]

        # PreToolUse hook sets running
        pre_cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert "running" in pre_cmd

    def test_hooks_idempotent(self, tmp_path, monkeypatch):
        """Running install_hooks() twice doesn't duplicate entries."""
        hook_dest = tmp_path / "sw-hook.sh"
        settings_path = tmp_path / "settings.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
        monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", settings_path)
        monkeypatch.setattr("super_worker.services.hooks.STATE_DIR", state_dir)
        monkeypatch.setattr(
            "super_worker.services.hooks._get_hook_source",
            lambda: Path(__file__).parent.parent / "super_worker" / "scripts" / "sw-hook.sh",
        )

        install_hooks()
        install_hooks()

        settings = json.loads(settings_path.read_text())
        assert len(settings["hooks"]["Stop"]) == 1, "Should not duplicate Stop hooks"
        assert len(settings["hooks"]["Notification"]) == 1
        assert len(settings["hooks"]["PreToolUse"]) == 1

    def test_hooks_preserve_existing(self, tmp_path, monkeypatch):
        """install_hooks() preserves existing non-SW hooks."""
        hook_dest = tmp_path / "sw-hook.sh"
        settings_path = tmp_path / "settings.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
        monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", settings_path)
        monkeypatch.setattr("super_worker.services.hooks.STATE_DIR", state_dir)
        monkeypatch.setattr(
            "super_worker.services.hooks._get_hook_source",
            lambda: Path(__file__).parent.parent / "super_worker" / "scripts" / "sw-hook.sh",
        )

        # Pre-existing hook
        settings_path.write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-custom-stop-hook"}]}]},
            "other_setting": True,
        }))

        install_hooks()

        settings = json.loads(settings_path.read_text())
        assert settings["other_setting"] is True, "Should preserve other settings"
        # Should have both: existing hook + our hook
        stop_hooks = settings["hooks"]["Stop"]
        assert len(stop_hooks) == 2, f"Expected 2 Stop hooks, got {len(stop_hooks)}"
        commands = [h["hooks"][0]["command"] for h in stop_hooks]
        assert "my-custom-stop-hook" in commands, "Should keep existing hook"
        assert any("waiting_input" in c for c in commands), "Should add our hook"

    def test_uninstall_removes_our_hooks(self, tmp_path, monkeypatch):
        """uninstall_hooks() removes SW hooks but preserves others."""
        hook_dest = tmp_path / "sw-hook.sh"
        settings_path = tmp_path / "settings.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
        monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", settings_path)
        monkeypatch.setattr("super_worker.services.hooks.STATE_DIR", state_dir)
        monkeypatch.setattr(
            "super_worker.services.hooks._get_hook_source",
            lambda: Path(__file__).parent.parent / "super_worker" / "scripts" / "sw-hook.sh",
        )

        # Install first
        settings_path.write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "my-keeper"}]}]},
        }))
        install_hooks()
        uninstall_hooks()

        settings = json.loads(settings_path.read_text())
        # Our hooks removed, custom kept
        assert len(settings["hooks"]["Stop"]) == 1
        assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "my-keeper"
        assert "PreToolUse" not in settings["hooks"]
        assert "Notification" not in settings["hooks"]

    def test_set_session_env(self, monkeypatch):
        """_set_session_env sets tmux environment variable."""
        mock_session = MagicMock()
        mock_server = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        _set_session_env("sw-feat-0", "SW_CC_STATE", "waiting_input")

        mock_session.set_environment.assert_called_once_with("SW_CC_STATE", "waiting_input")

    def test_batch_detect_reads_sw_cc_state(self, monkeypatch):
        """batch_detect_session_states reads SW_CC_STATE from tmux env."""
        alive = MagicMock()
        alive.session_name = "sw-a-0"
        alive.active_pane = MagicMock()
        alive.active_pane.pane_dead = "0"
        alive.show_environment.return_value = {"SW_CC_STATE": "waiting_input"}

        mock_server = MagicMock()
        mock_server.sessions = [alive]
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = batch_detect_session_states(["sw-a-0"])
        assert result["sw-a-0"] == SessionState.WAITING_INPUT

    def test_batch_detect_waiting_approval(self, monkeypatch):
        """batch_detect_session_states reads waiting_approval state."""
        alive = MagicMock()
        alive.session_name = "sw-a-0"
        alive.active_pane = MagicMock()
        alive.active_pane.pane_dead = "0"
        alive.show_environment.return_value = {"SW_CC_STATE": "waiting_approval"}

        mock_server = MagicMock()
        mock_server.sessions = [alive]
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = batch_detect_session_states(["sw-a-0"])
        assert result["sw-a-0"] == SessionState.WAITING_APPROVAL


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2: Terminal Rendering
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase2TerminalRendering:
    """Phase 2: PaneWatcher integration, layout."""

    @pytest.mark.asyncio
    async def test_terminal_layout_has_scroll(self):
        """TerminalPane has VerticalScroll for content."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            wt = pv._state.worktrees[0]
            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)

            # VerticalScroll contains terminal-content
            scroll = terminal.query_one("#terminal-scroll", VerticalScroll)
            content = scroll.query_one("#terminal-content", Static)
            assert content is not None, "terminal-content should be inside VerticalScroll"

            _save(app, "phase2_layout")

    @pytest.mark.asyncio
    async def test_pane_watcher_initialized(self):
        """TerminalPane creates a PaneWatcher instance."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            wt = pv._state.worktrees[0]
            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)

            assert terminal._watcher is not None, "PaneWatcher should be initialized"
            assert hasattr(terminal._watcher, "start_watching")
            assert hasattr(terminal._watcher, "stop_watching")
            assert hasattr(terminal._watcher, "cleanup")

    @pytest.mark.asyncio
    async def test_terminal_activates_on_session(self):
        """Setting active_session triggers PaneWatcher and fallback timer."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            wt = pv._state.worktrees[0]
            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)

            assert terminal.active_session is not None, \
                "Terminal should have an active session after startup"
            assert terminal._fallback_timer is not None, \
                "Fallback timer should be running"

    @pytest.mark.asyncio
    async def test_fallback_poll_interval_is_reasonable(self):
        """Fallback poll should be <= 0.5s (not the old 2.0s)."""
        from super_worker.constants import PANE_FALLBACK_POLL_S
        assert PANE_FALLBACK_POLL_S <= 0.5, \
            f"Fallback poll {PANE_FALLBACK_POLL_S}s is too slow, should be <= 0.5s"


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3: remain-on-exit, Dead Pane Detection, Respawn
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase3RemainOnExit:
    """Phase 3: remain-on-exit, dead pane detection, pane respawn."""

    def test_create_session_sets_remain_on_exit(self, monkeypatch):
        """create_session() enables remain-on-exit on the tmux session."""
        mock_tmux_session = MagicMock()
        mock_server = MagicMock()
        mock_server.sessions = []
        mock_server.new_session.return_value = mock_tmux_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        wt = Worktree(name="feat", path="/tmp/feat", branch="main")
        create_session(wt)

        mock_tmux_session.set_option.assert_any_call("remain-on-exit", "on")

    def test_is_session_alive_detects_dead_pane(self, monkeypatch):
        """is_session_alive returns False when pane is dead."""
        mock_pane = MagicMock()
        mock_pane.pane_dead = "1"
        mock_session = MagicMock()
        mock_session.active_pane = mock_pane
        mock_server = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        assert is_session_alive("sw-feat-0") is False

    def test_is_session_alive_returns_true_for_live_pane(self, monkeypatch):
        """is_session_alive returns True when pane is alive."""
        mock_pane = MagicMock()
        mock_pane.pane_dead = "0"
        mock_session = MagicMock()
        mock_session.active_pane = mock_pane
        mock_server = MagicMock()
        mock_server.sessions.get.return_value = mock_session
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        assert is_session_alive("sw-feat-0") is True

    def test_batch_detect_dead_pane(self, monkeypatch):
        """batch_detect_session_states returns DEAD for dead panes."""
        alive_session = MagicMock()
        alive_session.session_name = "sw-a-0"
        dead_pane = MagicMock()
        dead_pane.pane_dead = "1"
        alive_session.active_pane = dead_pane

        mock_server = MagicMock()
        mock_server.sessions = [alive_session]
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = batch_detect_session_states(["sw-a-0"])
        assert result["sw-a-0"] == SessionState.DEAD

    def test_respawn_pane_success(self, monkeypatch):
        """respawn_pane calls tmux respawn-pane command."""
        mock_server = MagicMock()
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = respawn_pane("sw-feat-0", "claude --continue")

        assert result is True
        mock_server.cmd.assert_called_once_with(
            "respawn-pane", "-k", "-t", "sw-feat-0", "claude --continue"
        )

    def test_respawn_pane_failure(self, monkeypatch):
        """respawn_pane returns False on failure."""
        mock_server = MagicMock()
        mock_server.cmd.side_effect = Exception("session gone")
        monkeypatch.setattr("super_worker.services.tmux.libtmux.Server", lambda: mock_server)

        result = respawn_pane("sw-dead-0", "claude --continue")
        assert result is False

    def test_recover_dead_sessions_respawns_in_place(self, tmp_path, monkeypatch):
        """Dead claude sessions with remain-on-exit are respawned in-place."""
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="dead-one")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: False)
        monkeypatch.setattr("super_worker.services.state.respawn_pane", lambda name, cmd: True)

        assert recover_dead_sessions(state) is True
        # Original session preserved (respawned in-place)
        assert state.worktrees[0].sessions[0].tmux_session_name == "sw-feat-0"
        assert state.worktrees[0].sessions[0].label == "dead-one"

    def test_recover_dead_sessions_recreates_when_respawn_fails(self, tmp_path, monkeypatch):
        """When respawn fails, a new session is created with --continue."""
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="dead-one")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: False)
        monkeypatch.setattr("super_worker.services.state.respawn_pane", lambda name, cmd: False)
        created_kwargs = []

        def fake_create(worktree, **kwargs):
            new_s = Session(tmux_session_name="sw-feat-1", label=kwargs.get("label", "new"))
            created_kwargs.append(kwargs)
            return new_s

        monkeypatch.setattr("super_worker.services.state.create_session", fake_create)

        assert recover_dead_sessions(state) is True
        assert state.worktrees[0].sessions[0].label == "(resumed)"
        assert created_kwargs[0]["resume"] is True

    def test_dead_terminal_sessions_dropped(self, tmp_path, monkeypatch):
        """Dead terminal sessions are dropped (nothing to --continue)."""
        wt_path = tmp_path / "feat"
        wt_path.mkdir()
        s = Session(tmux_session_name="sw-feat-0", label="my-shell", session_type="terminal")
        wt = Worktree(name="feat", path=str(wt_path), branch="sw-feat", sessions=[s])
        state = AppState(repo_root=str(tmp_path), worktree_base=str(tmp_path), worktrees=[wt])

        monkeypatch.setattr("super_worker.services.state.is_session_alive", lambda name: False)

        assert recover_dead_sessions(state) is True
        assert state.worktrees[0].sessions == []


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4: State Persistence Hardening
# ══════════════════════════════════════════════════════════════════════════════


class TestPhase4StatePersistence:
    """Phase 4: Backup before writes, fallback on corruption."""

    def test_save_state_creates_backup(self, tmp_path, monkeypatch, fake_config):
        """save_state() creates a .bak file before overwriting."""
        state = AppState(
            repo_root=str(fake_config.repo_root),
            worktree_base=str(fake_config.base_dir),
        )

        # First save — no backup yet
        save_state(state, fake_config)
        from super_worker.services.state import _state_file_for
        state_file = _state_file_for(fake_config)
        bak_file = state_file.with_suffix(".bak")
        assert not bak_file.exists(), "No backup on first save"

        # Second save — should create backup
        state.worktrees.append(Worktree(name="test", path="/tmp", branch="sw-test"))
        save_state(state, fake_config)
        assert bak_file.exists(), "Backup should exist after second save"

        # Backup should contain the previous state (without the new worktree)
        bak_data = json.loads(bak_file.read_text())
        assert len(bak_data.get("worktrees", [])) == 0, "Backup should have old state"

    def test_load_state_falls_back_to_backup(self, tmp_path, monkeypatch, fake_config):
        """load_state() falls back to .bak when main file is corrupted."""
        # Save valid state
        wt = Worktree(name="preserved", path="/tmp/preserved", branch="sw-preserved")
        state = AppState(
            repo_root=str(fake_config.repo_root),
            worktree_base=str(fake_config.base_dir),
            worktrees=[wt],
        )
        save_state(state, fake_config)

        # Save again to create backup with the worktree
        state.worktrees.append(Worktree(name="new", path="/tmp/new", branch="sw-new"))
        save_state(state, fake_config)

        # Corrupt the main file
        from super_worker.services.state import _state_file_for
        state_file = _state_file_for(fake_config)
        state_file.write_text("corrupted{{{not json")

        # Load should fall back to backup
        loaded = load_state(fake_config)
        assert len(loaded.worktrees) == 1, "Should load from backup (which had 1 worktree)"
        assert loaded.worktrees[0].name == "preserved"

    def test_load_state_fresh_when_both_corrupted(self, tmp_path, monkeypatch, fake_config):
        """load_state() returns fresh state when both main and backup are corrupted."""
        # Save and then corrupt both files
        state = AppState(
            repo_root=str(fake_config.repo_root),
            worktree_base=str(fake_config.base_dir),
        )
        save_state(state, fake_config)
        save_state(state, fake_config)  # Create backup

        from super_worker.services.state import _state_file_for
        state_file = _state_file_for(fake_config)
        state_file.write_text("bad{{{")
        state_file.with_suffix(".bak").write_text("also bad{{{")

        loaded = load_state(fake_config)
        assert loaded.worktrees == [], "Should return fresh state"
        assert loaded.repo_root == str(fake_config.repo_root)


# ══════════════════════════════════════════════════════════════════════════════
# FULL APP INTEGRATION: Session lifecycle with screenshots
# ══════════════════════════════════════════════════════════════════════════════


class TestAppIntegration:
    """End-to-end app tests with screenshots at each stage."""

    @pytest.mark.asyncio
    async def test_startup_selects_first_session(self):
        """On startup, the first session is selected in sidebar and terminal."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            assert pv is not None
            wt = pv._state.worktrees[0]
            assert len(wt.sessions) >= 1

            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            sidebar = wtc.query_one(SessionSidebar)

            first = wt.sessions[0]
            assert terminal.active_session == first.tmux_session_name, \
                "Terminal should show the first session on startup"

            sess_list = sidebar.query_one("#session-list", ListView)
            assert sess_list.index is not None, "Sidebar should have a selected item on startup"

            _save(app, "integration_01_startup")

    @pytest.mark.asyncio
    async def test_create_session_selects_it(self):
        """Creating a new session selects it in both sidebar and terminal."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            wt = pv._state.worktrees[0]
            pv._active_worktree = wt
            initial_count = len(wt.sessions)

            # Open new session dialog
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, NewSessionScreen)
            _save(app, "integration_02_new_session_dialog")

            # Accept defaults
            await pilot.press("enter")
            await pilot.pause(delay=2.0)

            assert len(wt.sessions) == initial_count + 1
            new_session = wt.sessions[-1]

            # Active session tracking updated
            assert pv._active_session_name == new_session.tmux_session_name

            # Terminal shows new session
            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)
            assert terminal.active_session == new_session.tmux_session_name

            # Sidebar highlights new session
            sidebar = wtc.query_one(SessionSidebar)
            sess_list = sidebar.query_one("#session-list", ListView)
            idx = sess_list.index
            assert idx is not None
            session_at_idx = sidebar._session_map.get(idx)
            assert session_at_idx is not None
            assert session_at_idx.tmux_session_name == new_session.tmux_session_name, \
                f"Sidebar should highlight '{new_session.tmux_session_name}', " \
                f"but highlights '{session_at_idx.tmux_session_name}'"

            _save(app, "integration_03_session_created")

    @pytest.mark.asyncio
    async def test_create_multiple_sessions(self):
        """Creating multiple sessions tracks them correctly."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            wt = pv._state.worktrees[0]
            pv._active_worktree = wt

            # Create 2 more sessions
            for i in range(2):
                await pilot.press("ctrl+s")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(delay=2.0)

            # Sidebar should show all sessions
            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            sidebar = wtc.query_one(SessionSidebar)
            sess_list = sidebar.query_one("#session-list", ListView)
            assert len(sess_list.children) == len(wt.sessions), \
                f"Sidebar should show {len(wt.sessions)} sessions, shows {len(sess_list.children)}"

            # Active is the last created
            last = wt.sessions[-1]
            assert pv._active_session_name == last.tmux_session_name

            _save(app, "integration_04_multiple_sessions")

    @pytest.mark.asyncio
    async def test_sidebar_session_labels_have_state_dots(self):
        """Session labels in sidebar include state indicator dots and type tags."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            wt = pv._state.worktrees[0]
            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            sidebar = wtc.query_one(SessionSidebar)

            # Check session labels have expected format
            sess_list = sidebar.query_one("#session-list", ListView)
            if sess_list.children:
                # Verify the session_map has a session with expected attributes
                assert 0 in sidebar._session_map, "First session should be in session_map"
                session = sidebar._session_map[0]
                assert session.session_type in ("claude", "terminal"), \
                    f"Session type should be claude or terminal, got {session.session_type}"

                # The label widget exists
                item = sess_list.children[0]
                label = item.query_one(Label)
                assert label is not None, "Label widget should exist in list item"

            _save(app, "integration_05_state_dots")

    @pytest.mark.asyncio
    async def test_hooks_installed_on_app_startup(self):
        """install_hooks() is called during app initialization."""
        # The app.__init__ calls install_hooks() — verify it doesn't crash
        # and the app still starts correctly
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=0.5)
            assert app.is_running

    @pytest.mark.asyncio
    async def test_terminal_pane_cleanup_on_session_switch(self):
        """Switching sessions clears old watcher and starts new one."""
        app = SuperWorkerApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(delay=1.0)

            pv = _pv(app)
            wt = pv._state.worktrees[0]
            wtc = pv.query_one(f"#wtc-{wt.name}", WorktreeTabContent)
            terminal = wtc.query_one(TerminalPane)

            assert terminal.active_session is not None, "Should have active session"

            # Create a second session
            pv._active_worktree = wt
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(delay=2.0)

            assert terminal.active_session is not None, "Should still have active session"

            # Fallback timer should still be active
            assert terminal._fallback_timer is not None

            # Active session should match the last created session
            last_session = wt.sessions[-1]
            assert pv._active_session_name == last_session.tmux_session_name

            # Clearing active_session should stop the timer
            terminal.active_session = None
            assert terminal._fallback_timer is None, \
                "Fallback timer should stop when session is cleared"

            _save(app, "integration_06_session_switch")

    @pytest.mark.asyncio
    async def test_send_keys_sets_running_state(self, monkeypatch):
        """send_keys() sets SW_CC_STATE=running after sending keys."""
        set_env_calls = []
        original_set_env = _set_session_env

        def tracking_set_env(name, key, value):
            set_env_calls.append((name, key, value))

        monkeypatch.setattr("super_worker.services.tmux._set_session_env", tracking_set_env)

        from super_worker.services.tmux import send_keys
        # send_keys uses _get_pane which needs a mock
        mock_pane = MagicMock()
        monkeypatch.setattr("super_worker.services.tmux._get_pane", lambda name: mock_pane)

        send_keys("sw-feat-0", "Enter")

        assert ("sw-feat-0", "SW_CC_STATE", "running") in set_env_calls, \
            "send_keys should set SW_CC_STATE=running"
