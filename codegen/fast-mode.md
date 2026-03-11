# Fast Mode — Native tmux Split Panes

## Status: Draft

## Outcome

`sw --fast` launches a polished, zero-overhead tmux-native UI with full feature parity to the Textual TUI. Every worktree, session, git, and project management feature works identically — just rendered through native tmux primitives instead of Textual widgets.

## Context

The Textual TUI captures tmux pane output, parses ANSI, hashes for change detection, and re-renders a Static widget. This causes input lag. Tools like cmux avoid this by using tmux's built-in split panes. Fast mode eliminates the rendering pipeline while preserving all functionality.

## Architecture

### Concept Mapping

| Concept | TUI mode | Fast mode |
|---|---|---|
| Project | ContentSwitcher tab | Separate tmux host session |
| Worktree | TabbedContent tab | tmux window in host session |
| CC instance | Separate tmux session | tmux pane within worktree window |
| Session state dots | Sidebar colored dots | Pane border icons + status bar |
| Git status | Sidebar section | Status bar left |
| Git actions (commit/push/pull/PR) | Sidebar buttons | `prefix+G` popup menu |
| New worktree dialog | Textual ModalScreen | `tmux display-popup` running `sw fast-wizard` |
| New session dialog | Textual ModalScreen | `tmux display-popup` running `sw fast-wizard` |
| Rename session | Textual ModalScreen | `tmux command-prompt` |
| Delete session | `x` key in sidebar | `prefix+X` with confirm |
| Delete worktree | Ctrl+D → confirm modal | `prefix+D` with confirm popup |
| Full attach | Ctrl+A (suspend TUI) | Native — you're already in the pane |
| Open in terminal | Ctrl+T (osascript) | Not needed — already in terminal |
| Project drawer | Ctrl+O overlay | `prefix+O` popup with `sw list-projects` |
| Project switching | Ctrl+Shift+arrows | Switch tmux sessions (prefix+( / prefix+)) |
| Settings | Ctrl+E → config modal | `prefix+E` popup running `sw config` |
| Keybinding hints | Footer bar | Status bar right |
| Attention indicator | 🔔 in tab label | Window name `!` prefix + bell |
| Session selection | Sidebar click | Native pane focus (mouse click or prefix+arrows) |
| Periodic refresh | Textual timer (5s) | tmux `status-interval` (2s) + hook-based pane titles |
| Default worktree | Auto-created on mount | Auto-created on launch |
| Dead session recovery | `recover_dead_sessions()` | Same on launch, skip during runtime |

### Host Session Layout

```
┌─────────────────────────────────────────────────────────────┐
│ [sw] super-worker │ main (fix/input-lag) ↑0↓2 ●  │ ^B? help│  ← status bar
├─────────────────────────────┬───────────────────────────────┤
│ ● session-0 [CC]            │ ○ session-1 [CC]              │  ← pane borders
│                             │                               │
│  Claude Code instance 1     │  Claude Code instance 2       │
│  (native terminal)          │  (native terminal)            │
│                             │                               │
│                             │                               │
├─────────────────────────────┴───────────────────────────────┤
│ [1:main] [2:feature-auth*] [3:refactor-api]                 │  ← window list
└─────────────────────────────────────────────────────────────┘
```

### Per-Pane State Detection

Current hooks set `SW_CC_STATE` per tmux session. In fast mode, multiple panes share one session. Solution:

- Each pane gets `SW_FAST_MODE=1` in its environment
- Hook script: if `SW_FAST_MODE` is set, uses `SW_CC_STATE_{TMUX_PANE}` (e.g., `SW_CC_STATE_%5`)
- Hook also updates the pane border title immediately for visual feedback
- Status bar script reads these suffixed env vars for aggregate display
- TUI mode unchanged (no `SW_FAST_MODE` set)

### Interactive Dialogs via `tmux display-popup`

TUI mode uses Textual ModalScreens for forms. Fast mode uses `tmux display-popup` to launch a lightweight Python CLI wizard (`sw fast-wizard`) that collects the same inputs:

```
┌─ New Worktree ──────────────────────┐
│                                     │
│  Name: feature-auth                 │
│  Branch (sw-feature-auth):          │
│  Prompt: /plan                      │
│  [ ] Detach HEAD                    │
│  [ ] Skip permissions               │
│                                     │
│  [Enter] Create  [Esc] Cancel       │
└─────────────────────────────────────┘
```

This runs inside `display-popup -w 50 -h 14 -E "sw fast-wizard new-worktree --host ..."`. The wizard is a simple Click-based interactive prompt — no Textual dependency. After collecting input, it calls the same backend functions (`create_worktree()`, `create_session()`) and updates state.

## Plan

### Step 1: Model changes
**File:** `super_worker/models.py` (lines 7-16, 29-34)

Add `tmux_pane_id` to Session (after `tmux_session_name`, line 12):
```python
tmux_pane_id: str | None = None  # e.g., "%5" — set in fast mode only
```

Add `ui_mode` to AppState (after `worktrees`, line 33):
```python
ui_mode: str = "tui"  # "tui" or "fast"
```

Both fields have defaults, so existing state files load without issues. `extra="ignore"` is already set on both models.

**Verify:** `python -c "from super_worker.models import AppState; print(AppState(repo_root='x', worktree_base='y').ui_mode)"` prints `tui`.

---

### Step 2: Constants
**File:** `super_worker/constants.py` (append after line 13)

```python
FAST_SESSION_PREFIX = "sw-fast"
FAST_STATUS_INTERVAL = 2  # seconds between tmux status bar refreshes
```

---

### Step 3: Update hook script for per-pane state + pane title
**File:** `super_worker/scripts/sw-hook.sh`

Replace lines 13-16 (the state-setting block) with:
```bash
if command -v tmux &>/dev/null && tmux has-session -t "$SW_SESSION_NAME" 2>/dev/null; then
    if [ -n "${SW_FAST_MODE:-}" ] && [ -n "${TMUX_PANE:-}" ]; then
        # Fast mode: per-pane state
        tmux setenv -t "$SW_SESSION_NAME" "SW_CC_STATE_${TMUX_PANE}" "$STATE"
        # Update pane border title for immediate visual feedback
        case "$STATE" in
            running)          ICON="●" ;;
            waiting_input)    ICON="○" ;;
            waiting_approval) ICON="◉" ;;
            *)                ICON="?" ;;
        esac
        tmux select-pane -t "$TMUX_PANE" -T "${ICON} ${SW_PANE_LABEL:-session} [${SW_PANE_TYPE:-CC}]"
    else
        # TUI mode: per-session state (unchanged)
        tmux setenv -t "$SW_SESSION_NAME" SW_CC_STATE "$STATE"
    fi
fi
```

**Verify:** In TUI mode, `env | grep SW_FAST` is empty → falls into else branch. In fast mode with `SW_FAST_MODE=1`, sets `SW_CC_STATE_%5` and updates pane title.

---

### Step 4: Create fast UI service module
**File:** `super_worker/services/fast_ui.py` (new file)

This is the core module. It manages the host session, layout, status bar, keybindings, and popup commands.

#### 4a: Imports and host session naming

```python
import logging
import os
import shlex
import subprocess
from pathlib import Path

import libtmux

from super_worker.config import ResolvedConfig
from super_worker.constants import FAST_SESSION_PREFIX, FAST_STATUS_INTERVAL
from super_worker.models import AppState, Session, Worktree
from super_worker.services.tmux import _get_server, SessionState, _STATE_MAP

logger = logging.getLogger(__name__)


def host_session_name(config: ResolvedConfig) -> str:
    """Deterministic host session name for a project."""
    return f"{FAST_SESSION_PREFIX}-{config.repo_root.name}"
```

#### 4b: `ensure_host_session(config) -> libtmux.Session`

- Check if session with `host_session_name(config)` exists
- If yes, return it
- If no, create with `server.new_session(session_name=name, start_directory=str(config.repo_root), window_command="")` and call `_configure_host_session(session, config)`
- Return the session

#### 4c: `_configure_host_session(session, config) -> None`

Set all tmux options that make it look polished. This runs once when the host session is created.

**Status bar:**
```python
project_name = config.repo_root.name
session.set_option("status", "on")
session.set_option("status-interval", str(FAST_STATUS_INTERVAL))
session.set_option("status-position", "top")

# Left: project name + current window git info
session.set_option("status-left", f" [sw] {project_name} │ ")
session.set_option("status-left-length", "40")
session.set_option("status-left-style", "bold")

# Right: keybinding hints
session.set_option("status-right", " ^B+N new wt │ ^B+S new sess │ ^B+G git │ ^B+? help ")
session.set_option("status-right-length", "80")

# Styling
session.set_option("status-style", "bg=#1e1e2e,fg=#cdd6f4")
session.set_option("pane-border-status", "top")
session.set_option("pane-border-format", " #{pane_title} ")
session.set_option("pane-active-border-style", "fg=#89b4fa")
session.set_option("pane-border-style", "fg=#45475a")
session.set_option("window-status-format", " #I:#W ")
session.set_option("window-status-current-format", " #I:#W* ")
session.set_option("window-status-current-style", "bold,fg=#89b4fa")
```

**Mouse:**
```python
session.set_option("mouse", "on")
```

**Keybindings** (via `server.cmd("bind-key", ...)`):
```python
sw = shutil.which("sw") or "sw"  # resolved path for stability
host = shlex.quote(session.session_name)

# New worktree — popup wizard
server.cmd("bind-key", "-T", "prefix", "N",
    "display-popup", "-w", "55", "-h", "14", "-E",
    f"{sw} fast-wizard new-worktree --host {host}")

# New session — popup wizard
server.cmd("bind-key", "-T", "prefix", "S",
    "display-popup", "-w", "55", "-h", "12", "-E",
    f"{sw} fast-wizard new-session --host {host} --window #{{window_name}}")

# Delete session pane
server.cmd("bind-key", "-T", "prefix", "X",
    "confirm-before", "-p", "Kill this session?",
    f"run-shell '{sw} fast-kill-pane --host {host} --pane #{{pane_id}}' \\; kill-pane")

# Delete worktree — popup confirm
server.cmd("bind-key", "-T", "prefix", "D",
    "display-popup", "-w", "50", "-h", "8", "-E",
    f"{sw} fast-wizard delete-worktree --host {host} --window #{{window_name}}")

# Git actions — popup menu
server.cmd("bind-key", "-T", "prefix", "G",
    "display-menu", "-T", "Git",
    "Commit", "c", f"display-popup -w 60 -h 8 -E '{sw} fast-wizard git-commit --window #{{window_name}}'",
    "Push",   "p", f"display-popup -w 60 -h 6 -E '{sw} fast-git push --window #{{window_name}}'",
    "Pull",   "l", f"display-popup -w 60 -h 6 -E '{sw} fast-git pull --window #{{window_name}}'",
    "Open PR","r", f"display-popup -w 60 -h 6 -E '{sw} fast-git pr --window #{{window_name}}'")

# Rename session — command prompt
server.cmd("bind-key", "-T", "prefix", "R",
    "command-prompt", "-p", "Rename session:",
    f"run-shell '{sw} fast-rename-pane --host {host} --pane #{{pane_id}} --label \"%%\"'")

# Settings — popup
server.cmd("bind-key", "-T", "prefix", "E",
    "display-popup", "-w", "70", "-h", "20", "-E",
    f"{sw} config")

# Help — popup showing all keybindings
server.cmd("bind-key", "-T", "prefix", "?",
    "display-popup", "-w", "55", "-h", "20", "-E",
    f"{sw} fast-help")

# Project list — popup
server.cmd("bind-key", "-T", "prefix", "O",
    "display-popup", "-w", "60", "-h", "15", "-E",
    f"{sw} fast-wizard switch-project")
```

#### 4d: `build_pane_cmd(session, worktree, host_name) -> str`

Build the shell command for a CC pane, including fast mode env vars:
```python
env_prefix = (
    f"SW_SESSION_NAME={shlex.quote(host_name)} "
    f"SW_FAST_MODE=1 "
    f"SW_PANE_LABEL={shlex.quote(session.label)} "
    f"SW_PANE_TYPE={'sh' if session.session_type == 'terminal' else 'CC'} "
    f"TERM=xterm-256color"
)
if session.session_type == "terminal":
    shell = os.environ.get("SHELL", "/bin/bash")
    return f"env {env_prefix} {shlex.quote(shell)}"
else:
    base = "claude --dangerously-skip-permissions" if session.skip_permissions else "claude"
    if session.initial_prompt:
        base = f"{base} {shlex.quote(session.initial_prompt)}"
    return f"env {env_prefix} {base}"
```

#### 4e: `create_worktree_window(host, worktree, first_session, host_name) -> tuple[libtmux.Window, str]`

Create a window for a worktree and return it with the pane ID:
```python
cmd = build_pane_cmd(first_session, worktree, host_name)
window = host.new_window(
    window_name=f"{worktree.name} ({worktree.branch})",
    start_directory=worktree.path,
    window_command=cmd,
)
pane_id = window.active_pane.pane_id
# Set initial pane title
server.cmd("select-pane", "-t", pane_id, "-T",
    f"● {first_session.label} [{('sh' if first_session.session_type == 'terminal' else 'CC')}]")
return window, pane_id
```

#### 4f: `add_pane_to_window(window, worktree, session, host_name) -> str`

Add a pane to an existing worktree window:
```python
cmd = build_pane_cmd(session, worktree, host_name)
pane = window.split_window(start_directory=worktree.path, shell=cmd)
window.select_layout("tiled")
server = _get_server()
server.cmd("select-pane", "-t", pane.pane_id, "-T",
    f"● {session.label} [{('sh' if session.session_type == 'terminal' else 'CC')}]")
return pane.pane_id
```

#### 4g: `detect_pane_states(host_name, pane_ids) -> dict[str, SessionState]`

Read per-pane states from the host session environment:
```python
server = _get_server()
session = server.sessions.get(session_name=host_name)
env = session.show_environment()
results = {}
for pane_id in pane_ids:
    key = f"SW_CC_STATE_{pane_id}"
    value = env.get(key, "")
    results[pane_id] = _STATE_MAP.get(value, SessionState.UNKNOWN)
return results
```

#### 4h: `launch(config, state) -> None`

Main entry point for fast mode:

```python
def launch(config: ResolvedConfig, state: AppState) -> None:
    from super_worker.services.state import save_state

    name = host_session_name(config)
    server = _get_server()

    # Check if host session already exists with content → just reattach
    try:
        existing = server.sessions.get(session_name=name)
        windows = existing.windows
        has_content = len(windows) > 1 or (
            len(windows) == 1 and windows[0].window_name != ""
        )
        if has_content:
            os.execvp("tmux", ["tmux", "attach-session", "-t", name])
            return
    except Exception:
        pass  # Session doesn't exist, create fresh

    host = ensure_host_session(config)

    # Ensure default worktree exists (same logic as TUI)
    _ensure_default_worktree(state, config)

    # Populate windows and panes from state
    for wt in state.worktrees:
        if not wt.sessions:
            # Create default session for worktree (same as TUI's WorktreeTabContent.on_mount)
            session = Session(
                tmux_session_name=name,
                label="session 0",
                session_type="claude",
            )
            wt.sessions.append(session)

        first = wt.sessions[0]
        first.tmux_session_name = name
        window, pane_id = create_worktree_window(host, wt, first, name)
        first.tmux_pane_id = pane_id

        for session in wt.sessions[1:]:
            session.tmux_session_name = name
            pid = add_pane_to_window(window, wt, session, name)
            session.tmux_pane_id = pid

    # Kill the placeholder window created by new_session
    placeholder = host.windows[0]
    if placeholder.window_name == "" and len(host.windows) > 1:
        placeholder.kill()

    # Save state with pane IDs and mode
    state.ui_mode = "fast"
    save_state(state, config)

    # Replace process with tmux attach
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])
```

#### 4i: `_ensure_default_worktree(state, config) -> None`

Same logic as `ProjectView._ensure_default_worktree()` but without Textual dependency:
```python
from super_worker.constants import DEFAULT_WORKTREE_NAME
from super_worker.services.worktree import get_current_branch

existing = state.get_worktree(DEFAULT_WORKTREE_NAME)
if existing:
    existing.branch = get_current_branch(str(config.repo_root))
    return

branch = get_current_branch(str(config.repo_root))
wt = Worktree(name=DEFAULT_WORKTREE_NAME, path=str(config.repo_root), branch=branch)
state.worktrees.insert(0, wt)
```

**Verify:** Run `sw --fast` from a repo → host session created → windows populated → tmux attached with panes.

---

### Step 5: CLI entry — `--fast` flag
**File:** `super_worker/cli.py` (modify `cli` function, lines 44-55)

Replace the `cli` function:
```python
@click.group(invoke_without_command=True)
@click.option("--fast", is_flag=True, help="Launch with native tmux panes (no TUI rendering)")
@click.pass_context
def cli(ctx: click.Context, fast: bool) -> None:
    """Super Worker — Claude Code Instance Manager for Git Worktrees."""
    _check_prerequisites()
    if ctx.invoked_subcommand is None:
        if fast:
            _require_git_repo()
            from super_worker.services.fast_ui import launch
            from super_worker.services.hooks import install_hooks

            install_hooks()
            config = load_config()
            state = load_state(config)
            update_projects_registry(config)
            reconcile_state(state, config)
            recover_dead_sessions(state)
            save_state(state, config)
            launch(config, state)
        else:
            from super_worker.app import SuperWorkerApp
            app = SuperWorkerApp()
            app.run()
```

**Verify:** `sw --fast` launches fast mode. `sw` launches TUI mode.

---

### Step 6: Fast wizard — interactive popups
**File:** `super_worker/services/fast_wizard.py` (new file)

This module provides Click-based interactive prompts that run inside `tmux display-popup`. No Textual dependency. Each wizard function collects input, calls backend services, saves state, and exits.

#### 6a: New worktree wizard

```python
def wizard_new_worktree(host_session: str) -> None:
    """Interactive prompt for creating a new worktree."""
    import re
    name = input("  Name: ").strip()
    if not name or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        print("  Invalid name. Letters, digits, hyphens, underscores only.")
        input("  Press Enter to close...")
        return

    config = load_config()
    branch_default = f"{config.branch_prefix}{name}"
    branch = input(f"  Branch ({branch_default}): ").strip() or None
    prompt = input("  Prompt (optional): ").strip() or None
    detach = input("  Detach HEAD? [y/N]: ").strip().lower() == "y"
    skip_perms = input("  Skip permissions? [y/N]: ").strip().lower() == "y"

    state = load_state(config)
    if state.get_worktree(name):
        print(f"  Worktree '{name}' already exists.")
        input("  Press Enter to close...")
        return

    try:
        wt = create_worktree(config, name, branch=branch, detach=detach,
                             worktree_index=len(state.worktrees))
    except BranchExistsError as e:
        use = input(f"  Branch '{e.branch}' exists. Use it? [Y/n]: ").strip().lower()
        if use == "n":
            return
        wt = create_worktree(config, name, branch=branch, use_existing_branch=True,
                             detach=detach, worktree_index=len(state.worktrees))

    state.worktrees.append(wt)

    # Create session and window
    session = Session(tmux_session_name=host_session, label=prompt or "session 0",
                      session_type="claude", initial_prompt=prompt, skip_permissions=skip_perms)
    cmd = build_pane_cmd(session, wt, host_session)

    server = _get_server()
    host = server.sessions.get(session_name=host_session)
    window, pane_id = create_worktree_window(host, wt, session, host_session)
    session.tmux_pane_id = pane_id
    wt.sessions.append(session)
    save_state(state, config)
    print(f"  Created worktree: {name}")
```

#### 6b: New session wizard

```python
def wizard_new_session(host_session: str, window_name: str) -> None:
    """Interactive prompt for adding a session to the current worktree window."""
    type_input = input("  Type [1=Claude, 2=Terminal] (1): ").strip()
    session_type = "terminal" if type_input == "2" else "claude"

    if session_type == "claude":
        prompt = input("  Prompt (optional): ").strip() or None
        skip_perms = input("  Skip permissions? [y/N]: ").strip().lower() == "y"
    else:
        prompt, skip_perms = None, False

    label = input("  Label (optional): ").strip() or None

    # Find worktree from window name (format: "name (branch)")
    wt_name = window_name.split(" (")[0]
    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(wt_name)
    if not wt:
        print(f"  Worktree '{wt_name}' not found.")
        return

    session = Session(
        tmux_session_name=host_session,
        label=label or f"session {len(wt.sessions)}",
        session_type=session_type,
        initial_prompt=prompt,
        skip_permissions=skip_perms,
    )

    server = _get_server()
    host = server.sessions.get(session_name=host_session)
    window = next(w for w in host.windows if w.window_name == window_name)
    pane_id = add_pane_to_window(window, wt, session, host_session)
    session.tmux_pane_id = pane_id
    wt.sessions.append(session)
    save_state(state, config)
    print(f"  Created session: {session.label}")
```

#### 6c: Delete worktree wizard

```python
def wizard_delete_worktree(host_session: str, window_name: str) -> None:
    """Confirm and delete a worktree."""
    wt_name = window_name.split(" (")[0]
    if wt_name == DEFAULT_WORKTREE_NAME:
        print("  Cannot delete the main worktree.")
        input("  Press Enter to close...")
        return

    confirm = input(f"  Delete worktree '{wt_name}'? All sessions will be killed. [y/N]: ").strip().lower()
    if confirm != "y":
        return

    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(wt_name)
    if not wt:
        return

    # Kill the tmux window (kills all panes in it)
    server = _get_server()
    host = server.sessions.get(session_name=host_session)
    window = next((w for w in host.windows if w.window_name == window_name), None)
    if window:
        window.kill()

    remove_worktree(state, wt_name, force=True)
    state = remove_worktree_from_state(state, wt_name)
    save_state(state, config)
    print(f"  Deleted worktree: {wt_name}")
```

#### 6d: Git commit wizard

```python
def wizard_git_commit(window_name: str) -> None:
    """Prompt for commit message and commit."""
    wt_name = window_name.split(" (")[0]
    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(wt_name)
    if not wt:
        return

    msg = input(f"  Commit message ({config.commit_placeholder}): ").strip()
    if not msg:
        print("  No message provided.")
        return

    import git as gitpython
    try:
        repo = gitpython.Repo(wt.path)
        repo.git.add("-u")
        repo.git.commit("-m", msg)
        print("  Committed.")
    except gitpython.GitCommandError as e:
        print(f"  Commit failed: {str(e.stderr or e)[:100]}")
    input("  Press Enter to close...")
```

#### 6e: Switch project wizard

```python
def wizard_switch_project() -> None:
    """Show project list and let user pick."""
    from super_worker.services.state import load_projects_registry
    projects = load_projects_registry()
    if not projects:
        print("  No known projects.")
        input("  Press Enter to close...")
        return

    print("  Projects:")
    for i, p in enumerate(projects, 1):
        print(f"  {i}. {Path(p).name}  ({p})")

    choice = input(f"\n  Select (1-{len(projects)}): ").strip()
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(projects):
            path = projects[idx]
            # Launch fast mode for selected project
            subprocess.run(["sw", "--fast"], cwd=path)
    except (ValueError, IndexError):
        print("  Invalid selection.")
```

---

### Step 7: Fast mode CLI subcommands
**File:** `super_worker/cli.py` (append after existing commands)

#### 7a: `fast-wizard` — dispatches to wizard functions

```python
@cli.command("fast-wizard", hidden=True)
@click.argument("action")
@click.option("--host", "host_session", default="")
@click.option("--window", "window_name", default="")
def fast_wizard(action: str, host_session: str, window_name: str) -> None:
    """Interactive wizards for fast mode (called by tmux popups)."""
    from super_worker.services.fast_wizard import (
        wizard_new_worktree, wizard_new_session,
        wizard_delete_worktree, wizard_git_commit,
        wizard_switch_project,
    )
    if action == "new-worktree":
        wizard_new_worktree(host_session)
    elif action == "new-session":
        wizard_new_session(host_session, window_name)
    elif action == "delete-worktree":
        wizard_delete_worktree(host_session, window_name)
    elif action == "git-commit":
        wizard_git_commit(window_name)
    elif action == "switch-project":
        wizard_switch_project()
```

#### 7b: `fast-git` — non-interactive git operations

```python
@cli.command("fast-git", hidden=True)
@click.argument("action")
@click.option("--window", "window_name", required=True)
def fast_git(action: str, window_name: str) -> None:
    """Git operations for fast mode (push/pull/pr)."""
    _require_git_repo()
    import git as gitpython

    wt_name = window_name.split(" (")[0]
    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(wt_name)
    if not wt:
        click.echo(f"Worktree '{wt_name}' not found.", err=True)
        raise SystemExit(1)

    repo = gitpython.Repo(wt.path)

    if action == "push":
        print(f"  Pushing {wt.branch} to {config.remote}...")
        try:
            repo.git.push("-u", config.remote, wt.branch)
            print("  Pushed.")
        except gitpython.GitCommandError as e:
            print(f"  Push failed: {str(e.stderr or e)[:100]}")
    elif action == "pull":
        print(f"  Pulling {config.main_branch} from {config.remote}...")
        try:
            repo.git.pull(config.remote, config.main_branch)
            print("  Pulled.")
        except gitpython.GitCommandError as e:
            print(f"  Pull failed: {str(e.stderr or e)[:100]}")
    elif action == "pr":
        print("  Creating PR...")
        result = subprocess.run(
            ["gh", "pr", "create", "--fill", "--head", wt.branch],
            cwd=wt.path, capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            print(f"  PR created: {url}")
        else:
            print(f"  PR failed: {(result.stderr or '')[:100]}")

    input("  Press Enter to close...")
```

#### 7c: `fast-kill-pane` — remove session from state on pane kill

```python
@cli.command("fast-kill-pane", hidden=True)
@click.option("--host", "host_session", required=True)
@click.option("--pane", "pane_id", required=True)
def fast_kill_pane(host_session: str, pane_id: str) -> None:
    """Remove a session from state when its pane is killed."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    for wt in state.worktrees:
        for s in wt.sessions:
            if s.tmux_pane_id == pane_id:
                wt.sessions.remove(s)
                save_state(state, config)
                return
```

#### 7d: `fast-rename-pane` — rename a session's label

```python
@cli.command("fast-rename-pane", hidden=True)
@click.option("--host", "host_session", required=True)
@click.option("--pane", "pane_id", required=True)
@click.option("--label", required=True)
def fast_rename_pane(host_session: str, pane_id: str, label: str) -> None:
    """Rename a session and update its pane title."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    for wt in state.worktrees:
        for s in wt.sessions:
            if s.tmux_pane_id == pane_id:
                s.label = label
                save_state(state, config)
                # Update pane title
                server = _get_server()
                tag = "sh" if s.session_type == "terminal" else "CC"
                server.cmd("select-pane", "-t", pane_id, "-T", f"● {label} [{tag}]")
                return
```

#### 7e: `fast-help` — keybinding reference

```python
@cli.command("fast-help", hidden=True)
def fast_help() -> None:
    """Show fast mode keybinding reference."""
    print("""
  Super Worker — Fast Mode Keybindings
  ─────────────────────────────────────

  Session Management
    ^B + N    New worktree
    ^B + S    New session (in current worktree)
    ^B + X    Kill current session
    ^B + D    Delete worktree
    ^B + R    Rename session

  Git
    ^B + G    Git actions menu (commit/push/pull/PR)

  Navigation
    ^B + n/p  Next/prev worktree (window)
    ^B + ←→↑↓ Switch between panes
    Mouse     Click to focus pane

  Other
    ^B + O    Switch project
    ^B + E    Edit settings
    ^B + ?    This help
    ^B + d    Detach (reattach with: sw --fast)

  Tip: All panes are native tmux — scroll, copy,
  and interact directly with zero overhead.
""")
    input("  Press Enter to close...")
```

---

### Step 8: Update state recovery for fast mode
**File:** `super_worker/services/state.py` (modify `recover_dead_sessions`, line 116)

Add early return for fast mode at the top of the function:
```python
def recover_dead_sessions(state: AppState) -> bool:
    if state.ui_mode == "fast":
        # Fast mode: panes are ephemeral. Dead panes are cleaned up on next launch.
        return False
    # ... existing TUI recovery logic unchanged
```

**Verify:** With `ui_mode="fast"`, `recover_dead_sessions` is a no-op.

---

### Step 9: Window name updates for git info + attention
**File:** `super_worker/services/fast_ui.py` — add `update_window_status()`

Called by the status bar refresh to update window names with git info and attention indicators:

```python
def update_window_names(config: ResolvedConfig, state: AppState, host_name: str) -> None:
    """Update window names with git info and attention indicators."""
    from super_worker.services.worktree import get_branch_status, get_worktree_dirty

    server = _get_server()
    try:
        host = server.sessions.get(session_name=host_name)
    except Exception:
        return

    env = host.show_environment()
    windows_by_name = {w.window_name.lstrip("! "): w for w in host.windows}

    for wt in state.worktrees:
        base_name = f"{wt.name} ({wt.branch})"
        window = windows_by_name.get(base_name) or windows_by_name.get(f"! {base_name}")
        if not window:
            continue

        # Check attention
        has_attention = False
        for s in wt.sessions:
            if s.tmux_pane_id:
                key = f"SW_CC_STATE_{s.tmux_pane_id}"
                if env.get(key) == "waiting_approval":
                    has_attention = True
                    break

        # Build window name
        try:
            status = get_branch_status(wt.path, config.remote, config.main_branch)
            dirty = get_worktree_dirty(wt.path)
            dirty_mark = "*" if dirty else ""
            prefix = "! " if has_attention else ""
            new_name = f"{prefix}{wt.name} ({wt.branch} ↑{status['ahead']}↓{status['behind']}){dirty_mark}"
            window.rename_window(new_name)
        except Exception:
            pass
```

Add a CLI command for the status bar to call this:

```python
@cli.command("fast-refresh", hidden=True)
def fast_refresh() -> None:
    """Refresh window names with git info (called periodically or manually)."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    from super_worker.services.fast_ui import update_window_names, host_session_name
    update_window_names(config, state, host_session_name(config))
```

Update the status bar right to periodically call this:
In `_configure_host_session`, update `status-right` to include `#(sw fast-refresh 2>/dev/null)` alongside the keybinding hints. The refresh runs silently every `status-interval` seconds.

---

### Step 10: Ensure `tmux.py` exports needed internals
**File:** `super_worker/services/tmux.py`

No structural changes needed. `fast_ui.py` imports `_get_server`, `SessionState`, and `_STATE_MAP` which are already defined. Add them to a public API if desired, but since fast_ui is internal, direct imports from the same package are fine.

Verify that `_STATE_MAP` is accessible (it's a module-level dict, not name-mangled).

---

## Feature Parity Checklist

| Feature | TUI | Fast | Implementation |
|---|---|---|---|
| Session state indicators | ✅ colored dots | ✅ pane border icons | Step 3 (hook script) |
| Git status (branch/ahead/behind/dirty) | ✅ sidebar | ✅ window name + status bar | Step 9 |
| Git commit | ✅ modal | ✅ popup wizard | Step 6d |
| Git push | ✅ button | ✅ popup menu → command | Step 7b |
| Git pull | ✅ button | ✅ popup menu → command | Step 7b |
| Open PR | ✅ button | ✅ popup menu → command | Step 7b |
| New worktree | ✅ Ctrl+N modal | ✅ prefix+N popup wizard | Step 6a |
| New session | ✅ Ctrl+S modal | ✅ prefix+S popup wizard | Step 6b |
| Rename session | ✅ Ctrl+R modal | ✅ prefix+R prompt | Step 7d |
| Delete session | ✅ x key | ✅ prefix+X confirm | Step 4c |
| Delete worktree | ✅ Ctrl+D confirm | ✅ prefix+D popup wizard | Step 6c |
| Full attach | ✅ Ctrl+A suspend | ✅ native (always attached) | N/A |
| Open in terminal | ✅ Ctrl+T | ✅ not needed | N/A |
| Session switching | ✅ sidebar click | ✅ pane focus (mouse/keys) | Native tmux |
| Worktree tabs | ✅ TabbedContent | ✅ tmux windows | Step 4e |
| Project switching | ✅ Ctrl+O drawer | ✅ prefix+O popup | Step 6e |
| Settings editor | ✅ Ctrl+E modal | ✅ prefix+E popup | Step 4c |
| Keybinding hints | ✅ footer | ✅ status bar + prefix+? | Steps 4c, 7e |
| Attention indicators | ✅ 🔔 in tab | ✅ `!` prefix in window name | Step 9 |
| Default worktree creation | ✅ on mount | ✅ on launch | Step 4i |
| Dead session recovery | ✅ on startup | ✅ on startup | Step 8 |
| Periodic refresh | ✅ 5s timer | ✅ 2s status-interval | Step 9 |
| State persistence | ✅ same file | ✅ same file | Step 1 |
| Mouse support | ✅ limited (widget clicks) | ✅ full (pane focus, scroll, select) | Step 4c |

## Verification Plan

1. `sw --fast` from repo with existing worktrees → host session, windows, panes populated correctly
2. `sw --fast` after detach → reattaches to existing host session (no duplicates)
3. `prefix+N` → popup wizard → creates worktree + window + pane
4. `prefix+S` → popup wizard → splits current window with new pane
5. `prefix+X` → confirm → kills pane, removes from state
6. `prefix+D` → confirm → kills window, removes worktree
7. `prefix+R` → prompt → renames pane title and updates state
8. `prefix+G` → menu → commit/push/pull/PR work correctly
9. CC enters waiting_input → pane border title shows `○`
10. CC enters waiting_approval → window name gets `!` prefix
11. `prefix+?` → help popup with all keybindings
12. `sw` (no --fast) → TUI mode works identically (no regressions)
13. State file works across both modes (switch between them)
14. Mouse click on pane → focuses it
15. Scroll within pane → native tmux scrollback

## Out of Scope

- Simultaneous TUI + fast mode on same project (pick one per session)
- Live migration of running sessions between modes
- Custom tmux prefix key configuration (use tmux's native `set -g prefix`)
- Status bar color theme configuration (hardcoded to Catppuccin Mocha palette, can be customized later)
