import json
from pathlib import Path

from super_worker.services.hooks import install_hooks, uninstall_hooks


def _setup_hooks_env(tmp_path, monkeypatch):
    """Common setup: point hooks module at tmp_path for isolation."""
    hook_dest = tmp_path / "sw-hook.sh"
    claude_settings = tmp_path / ".claude" / "settings.json"

    # Mock _get_hook_source to return a fake script
    hook_source = tmp_path / "source" / "sw-hook.sh"
    hook_source.parent.mkdir(parents=True)
    hook_source.write_text("#!/bin/bash\necho test")
    monkeypatch.setattr("super_worker.services.hooks._get_hook_source", lambda: hook_source)

    monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
    monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", claude_settings)
    monkeypatch.setattr("super_worker.services.hooks.STATE_DIR", tmp_path)
    return hook_dest, claude_settings


def test_install_hooks_creates_script_and_settings(tmp_path, monkeypatch):
    """install_hooks() copies the script and adds hooks to settings.json."""
    hook_dest, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)

    install_hooks()

    assert hook_dest.exists()
    assert claude_settings.exists()

    settings = json.loads(claude_settings.read_text())
    assert "hooks" in settings
    assert "Stop" in settings["hooks"]
    assert "PermissionRequest" in settings["hooks"]
    assert "PreToolUse" in settings["hooks"]

    # Verify hook commands reference our script
    stop_hooks = settings["hooks"]["Stop"]
    assert len(stop_hooks) == 1
    assert "sw-hook.sh" in stop_hooks[0]["hooks"][0]["command"]
    assert "waiting_input" in stop_hooks[0]["hooks"][0]["command"]


def test_install_hooks_idempotent(tmp_path, monkeypatch):
    """Running install_hooks() twice doesn't duplicate hook entries."""
    _setup_hooks_env(tmp_path, monkeypatch)

    install_hooks()
    install_hooks()

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 1
    assert len(settings["hooks"]["PermissionRequest"]) == 1
    assert len(settings["hooks"]["PreToolUse"]) == 1


def test_install_hooks_preserves_existing(tmp_path, monkeypatch):
    """install_hooks() preserves existing settings and hooks."""
    hook_dest, claude_settings = _setup_hooks_env(tmp_path, monkeypatch)
    claude_settings.parent.mkdir(parents=True, exist_ok=True)

    # Pre-existing settings with a custom hook
    existing = {
        "apiKey": "sk-test",
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "echo custom"}]}
            ]
        }
    }
    claude_settings.write_text(json.dumps(existing))

    install_hooks()

    settings = json.loads(claude_settings.read_text())
    # Preserved existing key
    assert settings["apiKey"] == "sk-test"
    # Preserved existing custom hook + added ours
    stop_hooks = settings["hooks"]["Stop"]
    assert len(stop_hooks) == 2
    assert stop_hooks[0]["hooks"][0]["command"] == "echo custom"


def test_uninstall_hooks_removes_our_entries(tmp_path, monkeypatch):
    """uninstall_hooks() removes SW hooks but preserves others."""
    hook_dest = tmp_path / "sw-hook.sh"
    hook_dest.write_text("#!/bin/bash")
    claude_settings = tmp_path / ".claude" / "settings.json"
    claude_settings.parent.mkdir(parents=True)

    settings = {
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "echo custom"}]},
                {"hooks": [{"type": "command", "command": f"{hook_dest} waiting_input"}]},
            ],
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": f"{hook_dest} running"}]},
            ],
        }
    }
    claude_settings.write_text(json.dumps(settings))

    monkeypatch.setattr("super_worker.services.hooks._HOOK_DEST", hook_dest)
    monkeypatch.setattr("super_worker.services.hooks._CLAUDE_SETTINGS", claude_settings)

    uninstall_hooks()

    result = json.loads(claude_settings.read_text())
    # Custom hook preserved
    assert len(result["hooks"]["Stop"]) == 1
    assert result["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo custom"
    # PreToolUse removed entirely (was only our hook)
    assert "PreToolUse" not in result["hooks"]
    # Hook script removed
    assert not hook_dest.exists()
