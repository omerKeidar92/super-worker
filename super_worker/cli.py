import shutil
import sys

import click

from super_worker.config import load_config, load_toml, save_project_config
from super_worker.constants import format_pane_title
from super_worker.services.state import load_state, remove_worktree_from_state, save_state, update_projects_registry
from super_worker.services.tmux import create_session, is_session_alive, kill_all_sessions
from super_worker.services.worktree import (
    BranchExistsError,
    create_worktree,
    get_branch_status,
    get_worktree_dirty,
    remove_worktree,
)


def _check_prerequisites() -> None:
    """Verify tmux and claude CLI are available, exit with helpful message if not."""
    missing = []
    if not shutil.which("tmux"):
        missing.append("tmux — install via: brew install tmux (macOS) or apt install tmux (Linux)")
    if not shutil.which("claude"):
        missing.append("claude — install via: npm install -g @anthropic-ai/claude-code")
    if missing:
        click.echo("Missing required tools:\n", err=True)
        for m in missing:
            click.echo(f"  • {m}", err=True)
        click.echo("\nSee: https://github.com/okeidar/super-worker#prerequisites", err=True)
        sys.exit(1)


def _require_git_repo() -> None:
    """Exit with a clear message if not inside a git repository."""
    from super_worker.config import detect_repo_root
    try:
        detect_repo_root()
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        click.echo("Tip: run 'sw' without a subcommand from any directory to open the TUI.", err=True)
        sys.exit(1)


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
            from super_worker.services.state import load_and_reconcile

            install_hooks()
            config = load_config()
            state = load_and_reconcile(config)
            launch(config, state)
        else:
            # Lazy import: SuperWorkerApp pulls in Textual, which is slow to load.
            # CLI-only commands (new, list, cleanup, config) skip this cost.
            from super_worker.app import SuperWorkerApp

            app = SuperWorkerApp()
            app.run()


@cli.command()
@click.argument("name")
@click.option("--branch", "-b", default=None, help="Branch name (defaults to name)")
@click.option("--prompt", "-p", default=None, help="Initial prompt or skill for Claude Code")
@click.option("--skip-permissions", "-s", is_flag=True, help="Launch Claude Code with --dangerously-skip-permissions")
def new(name: str, branch: str | None, prompt: str | None, skip_permissions: bool) -> None:
    """Create a new worktree and optionally launch a Claude Code session."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    update_projects_registry(config)

    if state.get_worktree(name):
        click.echo(f"Worktree '{name}' already exists.", err=True)
        raise SystemExit(1)

    try:
        wt = create_worktree(config, name, branch, worktree_index=len(state.worktrees))
    except BranchExistsError as e:
        if click.confirm(f"Branch '{e.branch}' already exists. Use it?"):
            wt = create_worktree(config, name, branch, use_existing_branch=True, worktree_index=len(state.worktrees))
        else:
            click.echo("Aborted.", err=True)
            raise SystemExit(1)
    except FileExistsError as e:
        click.echo(str(e), err=True)
        raise SystemExit(1)

    state.worktrees.append(wt)
    click.echo(f"Created worktree: {wt.path} (branch: {wt.branch})")

    if prompt or skip_permissions:
        session = create_session(wt, prompt=prompt, label=prompt, skip_permissions=skip_permissions)
        wt.sessions.append(session)
        click.echo(f"Launched session: {session.tmux_session_name} ({session.label})")

    save_state(state, config)


@cli.command("add")
@click.argument("worktree_name")
@click.option("--prompt", "-p", default=None, help="Initial prompt or skill for Claude Code")
@click.option("--label", "-l", default=None, help="Session label")
@click.option("--skip-permissions", "-s", is_flag=True, help="Launch Claude Code with --dangerously-skip-permissions")
def add_session(worktree_name: str, prompt: str | None, label: str | None, skip_permissions: bool) -> None:
    """Add a new CC session to an existing worktree."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(worktree_name)
    if not wt:
        click.echo(f"Worktree '{worktree_name}' not found.", err=True)
        raise SystemExit(1)

    session = create_session(wt, prompt=prompt, label=label, skip_permissions=skip_permissions)
    wt.sessions.append(session)
    click.echo(f"Launched session: {session.tmux_session_name} ({session.label})")
    save_state(state, config)


@cli.command("list")
def list_cmd() -> None:
    """List all worktrees and their sessions."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    if not state.worktrees:
        click.echo("No worktrees.")
        return

    for wt in state.worktrees:
        status = get_branch_status(wt.path, config.remote, config.main_branch)
        dirty = get_worktree_dirty(wt.path)
        dirty_marker = " *" if dirty else ""
        status_str = f"↑{status['ahead']} ↓{status['behind']}"
        click.echo(f"\n{wt.name} ({wt.branch}) [{status_str}]{dirty_marker}")
        click.echo(f"  path: {wt.path}")
        if not wt.sessions:
            click.echo("  (no sessions)")
        for s in wt.sessions:
            alive = "alive" if is_session_alive(s.tmux_session_name) else "exited"
            click.echo(f"  {s.label} [{alive}] — {s.tmux_session_name}")


@cli.command()
@click.argument("name")
@click.option("--force", "-f", is_flag=True, help="Force remove even with uncommitted changes")
def cleanup(name: str, force: bool) -> None:
    """Kill all sessions and remove a worktree."""
    _require_git_repo()
    config = load_config()
    state = load_state(config)
    wt = state.get_worktree(name)
    if not wt:
        click.echo(f"Worktree '{name}' not found.", err=True)
        raise SystemExit(1)

    kill_all_sessions(wt)
    click.echo(f"Killed {len(wt.sessions)} session(s).")

    try:
        remove_worktree(state, name, force=force)
        click.echo(f"Removed worktree: {wt.path}")
    except RuntimeError as e:
        click.echo(str(e), err=True)
        raise SystemExit(1)

    state = remove_worktree_from_state(state, name)
    save_state(state, config)


@cli.command()
@click.argument("key", required=False)
@click.argument("value", required=False)
def config(key: str | None, value: str | None) -> None:
    """View or edit project settings (.sw.toml).

    With no args: show current config.
    With KEY: show a specific value.
    With KEY VALUE: set a value (e.g. `sw config worktree.branch_prefix sc-`).
    """
    _require_git_repo()
    resolved = load_config()
    project_cfg = load_toml(resolved.repo_root / ".sw.toml")

    if key is None:
        # Show all resolved config
        click.echo(f"Project: {resolved.repo_root}")
        click.echo(f"Config:  {resolved.repo_root / '.sw.toml'}\n")
        click.echo("[worktree]")
        click.echo(f"  prefix        = {resolved.worktree_prefix}")
        click.echo(f"  branch_prefix = {resolved.branch_prefix}")
        click.echo(f"  base_dir      = {resolved.base_dir}")
        click.echo("\n[env]")
        click.echo(f"  symlinks         = {resolved.symlinks}")
        click.echo(f"  copies           = {resolved.copies}")
        click.echo(f"  post_create_hook = {resolved.post_create_hook or '(none)'}")
        click.echo("\n[git]")
        click.echo(f"  main_branch = {resolved.main_branch}")
        click.echo(f"  remote      = {resolved.remote}")
        click.echo("\n[ui]")
        click.echo(f"  commit_placeholder = {resolved.commit_placeholder}")
        click.echo(f"  name_placeholder   = {resolved.name_placeholder}")
        click.echo(f"  branch_placeholder = {resolved.branch_placeholder}")
        return

    if "." not in key:
        click.echo("Key must be section.field (e.g. worktree.branch_prefix)", err=True)
        raise SystemExit(1)

    section_name, field_name = key.split(".", 1)
    section = getattr(project_cfg, section_name, None)
    if section is None or field_name not in section.model_fields:
        click.echo(f"Unknown config key: {key}", err=True)
        raise SystemExit(1)

    if value is None:
        # Show specific value
        click.echo(getattr(section, field_name))
        return

    # Set value
    field_type = type(getattr(section, field_name))
    if field_type is list:
        parsed_value = [s.strip() for s in value.split(",") if s.strip()]
    else:
        parsed_value = value
    setattr(section, field_name, parsed_value)
    path = save_project_config(resolved.repo_root, project_cfg)
    click.echo(f"Set {key} = {parsed_value}")
    click.echo(f"Saved to {path}")


# ── Fast mode subcommands (called by tmux keybindings/popups) ────────────


@cli.command("fast-wizard", hidden=True)
@click.argument("action")
@click.option("--host", "host_session", default="")
@click.option("--window", "window_name", default="")
def fast_wizard(action: str, host_session: str, window_name: str) -> None:
    """Interactive wizards for fast mode (called by tmux popups)."""
    from super_worker.services.fast_wizard import (
        wizard_delete_worktree,
        wizard_git_commit,
        wizard_new_session,
        wizard_new_worktree,
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


@cli.command("fast-git", hidden=True)
@click.argument("action")
@click.option("--window", "window_name", required=True)
def fast_git(action: str, window_name: str) -> None:
    """Git operations for fast mode (push/pull/pr)."""
    _require_git_repo()
    from super_worker.services.fast_ui import worktree_name_from_window
    from super_worker.services.worktree import git_create_pr, git_pull, git_push

    wt_name = worktree_name_from_window(window_name)
    cfg = load_config()
    state = load_state(cfg)
    wt = state.get_worktree(wt_name)
    if not wt:
        click.echo(f"Worktree '{wt_name}' not found.", err=True)
        raise SystemExit(1)

    if action == "push":
        print(f"  Pushing {wt.branch} to {cfg.remote}...")
        err = git_push(wt.path, cfg.remote, wt.branch)
        print(f"  Push failed: {err}" if err else "  Pushed.")
    elif action == "pull":
        print(f"  Pulling {cfg.main_branch} from {cfg.remote}...")
        err = git_pull(wt.path, cfg.remote, cfg.main_branch)
        print(f"  Pull failed: {err}" if err else "  Pulled.")
    elif action == "pr":
        print("  Creating PR...")
        ok, result = git_create_pr(wt.path, wt.branch)
        print(f"  PR created: {result}" if ok else f"  PR failed: {result}")

    input("  Press Enter to close...")


@cli.command("fast-kill-pane", hidden=True)
@click.option("--host", "host_session", required=True)
@click.option("--pane", "pane_id", required=True)
def fast_kill_pane(host_session: str, pane_id: str) -> None:
    """Remove a session from state when its pane is killed."""
    _require_git_repo()
    cfg = load_config()
    state = load_state(cfg)
    match = state.find_session_by_pane_id(pane_id)
    if match:
        wt, s = match
        wt.sessions.remove(s)
        save_state(state, cfg)


@cli.command("fast-rename-pane", hidden=True)
@click.option("--host", "host_session", required=True)
@click.option("--pane", "pane_id", required=True)
@click.option("--label", required=True)
def fast_rename_pane(host_session: str, pane_id: str, label: str) -> None:
    """Rename a session and update its pane title."""
    _require_git_repo()
    from super_worker.services.tmux import _get_server

    cfg = load_config()
    state = load_state(cfg)
    match = state.find_session_by_pane_id(pane_id)
    if match:
        _, s = match
        s.label = label
        save_state(state, cfg)
        server = _get_server()
        server.cmd("select-pane", "-t", pane_id, "-T", format_pane_title(label, s.session_type))


@cli.command("fast-refresh", hidden=True)
def fast_refresh() -> None:
    """Refresh window names with git info (called by tmux status-interval)."""
    try:
        from super_worker.config import detect_repo_root

        detect_repo_root()
    except RuntimeError:
        return
    cfg = load_config()
    state = load_state(cfg)
    from super_worker.services.fast_ui import host_session_name, update_window_names

    update_window_names(cfg, state, host_session_name(cfg))


@cli.command("fast-respawn-pane", hidden=True)
@click.option("--host", "host_session", required=True)
@click.option("--pane", "pane_id", required=True)
@click.option("--window", "window_name", required=True)
def fast_respawn_pane(host_session: str, pane_id: str, window_name: str) -> None:
    """Respawn a dead pane with claude --continue."""
    _require_git_repo()
    from super_worker.services.fast_ui import build_pane_cmd, make_fast_session, worktree_name_from_window
    from super_worker.services.tmux import _get_server

    cfg = load_config()
    state = load_state(cfg)
    server = _get_server()

    match = state.find_session_by_pane_id(pane_id)
    if match:
        wt, s = match
        if s.session_type == "claude":
            cmd = build_pane_cmd(s, wt, host_session, resume=True)
            server.cmd("respawn-pane", "-k", "-t", pane_id, cmd)
            return

    # Fallback: build a minimal resume command
    from super_worker.models import Worktree as WtModel
    wt_name = worktree_name_from_window(window_name)
    fallback_session = make_fast_session(host_session, label="resumed")
    fallback_wt = WtModel(name=wt_name, path=".", branch="")
    cmd = build_pane_cmd(fallback_session, fallback_wt, host_session, resume=True)
    server.cmd("respawn-pane", "-k", "-t", pane_id, cmd)


@cli.command("fast-open-terminal", hidden=True)
@click.option("--host", "host_session", required=True)
def fast_open_terminal(host_session: str) -> None:
    """Open a new terminal emulator window attached to the host session."""
    from super_worker.services.tmux import open_external_terminal
    open_external_terminal(host_session)


@cli.command("fast-help", hidden=True)
def fast_help() -> None:
    """Show fast mode keybinding reference."""
    print("""
  Super Worker \u2014 Fast Mode
  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

  How it works:
    Worktree = tab at the top  (one branch each)
    Session  = pane in a tab   (Claude or terminal)

  \u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581\u2581

  Main menu:  Ctrl+B  then  Space
  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  Opens a menu with everything: create/delete
  worktrees, add sessions, git ops, projects,
  settings, and more. Just pick from the list.

  Quick shortcuts (all: Ctrl+B, then key):
    g         Git menu (commit/push/pull/PR)
    n / p     Next / previous worktree tab
    arrows    Move between panes
    d         Detach (reattach: sw --fast)

  Navigation:
    Click any pane with the mouse to focus it.
    Tabs at the top show worktree + git status.

  Tip: Panes render natively \u2014 zero overhead.
  Scroll, copy, and resize just like regular tmux.
""")
    input("  Press Enter to close...")
