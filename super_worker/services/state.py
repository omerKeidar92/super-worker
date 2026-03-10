import fcntl
import json
import logging
import shutil
from contextlib import contextmanager
from pathlib import Path

from super_worker.config import ResolvedConfig, detect_repo_root
from super_worker.constants import STATE_DIR
from super_worker.models import AppState
from super_worker.services.tmux import build_process_cmd, build_session_env_cmd, create_session, is_session_alive, respawn_pane, _get_server
from super_worker.services.worktree import discover_worktrees, get_current_branch, prune_git_cache

logger = logging.getLogger(__name__)


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _file_lock(path: Path, exclusive: bool = True):
    """Acquire a file lock (shared or exclusive) with automatic cleanup."""
    lock_file = path.with_suffix(".lock")
    with open(lock_file, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _state_file_for(config: ResolvedConfig) -> Path:
    """Per-repo state file keyed by repo root path hash."""
    return STATE_DIR / f"state-{config.state_hash}.json"


def _migrate_data(data: dict) -> dict:
    """Handle backward-compatible field renames."""
    if "repo_path" in data and "repo_root" not in data:
        data["repo_root"] = data.pop("repo_path")
    return data


def load_state(config: ResolvedConfig) -> AppState:
    _ensure_state_dir()
    state_file = _state_file_for(config)

    # Try legacy state.json if per-repo file doesn't exist,
    # but only if it belongs to this repo
    legacy_file = STATE_DIR / "state.json"
    if not state_file.exists() and legacy_file.exists():
        try:
            legacy_data = json.loads(legacy_file.read_text())
            legacy_root = legacy_data.get("repo_root") or legacy_data.get("repo_path", "")
            if str(config.repo_root) == legacy_root:
                state_file = legacy_file
        except (json.JSONDecodeError, OSError):
            logger.debug("Failed to read legacy state file, starting fresh")

    if not state_file.exists():
        return AppState(
            repo_root=str(config.repo_root),
            worktree_base=str(config.base_dir),
        )
    with _file_lock(state_file, exclusive=False):
        try:
            data = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            # Main file corrupted — try backup
            bak = state_file.with_suffix(".bak")
            if bak.exists():
                logger.warning("State file corrupted, falling back to backup")
                try:
                    data = json.loads(bak.read_text())
                except (json.JSONDecodeError, OSError):
                    logger.warning("Backup also corrupted, starting fresh")
                    return AppState(
                        repo_root=str(config.repo_root),
                        worktree_base=str(config.base_dir),
                    )
            else:
                logger.warning("State file corrupted and no backup, starting fresh")
                return AppState(
                    repo_root=str(config.repo_root),
                    worktree_base=str(config.base_dir),
                )

    data = _migrate_data(data)
    return AppState.model_validate(data)


def save_state(state: AppState, config: ResolvedConfig) -> None:
    _ensure_state_dir()
    state_file = _state_file_for(config)
    tmp = state_file.with_suffix(".tmp")
    with _file_lock(state_file):
        # Backup existing state file before overwriting
        if state_file.exists():
            shutil.copy2(state_file, state_file.with_suffix(".bak"))
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.rename(state_file)


def remove_worktree_from_state(state: AppState, name: str) -> AppState:
    state.worktrees = [wt for wt in state.worktrees if wt.name != name]
    return state


def remove_session_from_state(state: AppState, worktree_name: str, session_id: str) -> AppState:
    wt = state.get_worktree(worktree_name)
    if wt:
        wt.sessions = [s for s in wt.sessions if s.id != session_id]
    return state


def recover_dead_sessions(state: AppState) -> bool:
    """Recover dead sessions by respawning or recreating with `claude --continue`.

    For claude sessions where the tmux session still exists (remain-on-exit),
    respawn the pane in-place to preserve scrollback. Otherwise, create a new
    session. Dead terminal sessions are dropped (nothing to resume).

    Returns True if any sessions were recovered.
    """
    if state.ui_mode == "fast":
        # Fast mode: panes are ephemeral. Dead panes are cleaned up on next launch.
        return False
    changed = False
    for wt in state.worktrees:
        if not Path(wt.path).exists():
            continue
        alive = []
        dead_claude = []
        dead_other = []
        for s in wt.sessions:
            if is_session_alive(s.tmux_session_name):
                alive.append(s)
            elif s.session_type == "claude":
                dead_claude.append(s)
            else:
                dead_other.append(s)
        if not dead_claude and not dead_other:
            continue

        logger.info(
            "Recovering dead sessions in worktree",
            extra={"worktree": wt.name, "dead_claude": len(dead_claude), "dead_other": len(dead_other), "alive": len(alive)},
        )

        # Resume dead claude sessions; drop dead terminal sessions (nothing to resume)
        new_sessions = list(alive)
        for s in dead_claude:
            # Try to respawn in-place (preserves scrollback from remain-on-exit)
            process_cmd = build_process_cmd(
                session_type=s.session_type,
                skip_permissions=s.skip_permissions,
                resume=True,
            )
            resume_cmd = build_session_env_cmd(s.tmux_session_name, process_cmd)
            if respawn_pane(s.tmux_session_name, resume_cmd):
                logger.info("Respawned dead pane in-place", extra={"session": s.tmux_session_name})
                new_sessions.append(s)
            else:
                # Session gone entirely — create fresh with --continue
                resumed = create_session(wt, label="(resumed)", skip_permissions=False, resume=True)
                new_sessions.append(resumed)
        wt.sessions = new_sessions
        changed = True
    return changed


def _ensure_remain_on_exit(state: AppState) -> None:
    """Retroactively set remain-on-exit on all existing tmux sessions."""
    try:
        server = _get_server()
        live = {s.session_name: s for s in server.sessions}
    except Exception:
        return
    for wt in state.worktrees:
        for s in wt.sessions:
            tmux_sess = live.get(s.tmux_session_name)
            if tmux_sess is not None:
                try:
                    tmux_sess.set_option("remain-on-exit", "on")
                except Exception:
                    pass


def ensure_default_worktree(state: AppState, config: ResolvedConfig) -> bool:
    """Ensure the 'main' worktree exists, pointing at the repo root.

    Returns True if a new worktree was created (i.e. state changed).
    Both TUI and fast mode call this; callers can add sessions afterward.
    """
    from super_worker.constants import DEFAULT_WORKTREE_NAME
    from super_worker.models import Worktree

    existing = state.get_worktree(DEFAULT_WORKTREE_NAME)
    if existing:
        existing.branch = get_current_branch(str(config.repo_root))
        return False

    branch = get_current_branch(str(config.repo_root))
    wt = Worktree(name=DEFAULT_WORKTREE_NAME, path=str(config.repo_root), branch=branch)
    state.worktrees.insert(0, wt)
    return True


def reconcile_state(state: AppState, config: ResolvedConfig | None = None) -> bool:
    """Prune worktrees whose paths no longer exist, discover new ones. Returns True if changed."""
    changed = False

    valid_worktrees = []
    for wt in state.worktrees:
        if not Path(wt.path).exists():
            changed = True
            continue
        valid_worktrees.append(wt)
    state.worktrees = valid_worktrees
    prune_git_cache({wt.path for wt in valid_worktrees})

    # Ensure remain-on-exit is set on all existing sessions
    _ensure_remain_on_exit(state)

    # Discover worktrees on disk that aren't in state
    if config is not None:
        known_paths = {wt.path for wt in state.worktrees}
        for wt in discover_worktrees(config):
            if wt.path not in known_paths:
                logger.info("Discovered worktree on disk", extra={"name": wt.name, "path": wt.path})
                state.worktrees.append(wt)
                changed = True

    return changed


def _load_registry_json() -> list[str]:
    """Load projects.json with error handling. Returns empty list on any failure."""
    registry_path = STATE_DIR / "projects.json"
    if not registry_path.exists():
        return []
    try:
        return json.loads(registry_path.read_text())
    except (json.JSONDecodeError, TypeError):
        return []


def _normalize_registry(projects: list[str]) -> list[str]:
    """Resolve any worktree paths to their main repo root and deduplicate."""
    seen: list[str] = []
    for p in projects:
        path = Path(p)
        if not path.exists():
            continue
        try:
            normalized = str(detect_repo_root(path))
        except RuntimeError:
            continue
        if normalized not in seen:
            seen.append(normalized)
    return seen


def update_projects_registry(config: ResolvedConfig) -> None:
    """Track this repo in the global projects registry."""
    _ensure_state_dir()
    registry_path = STATE_DIR / "projects.json"
    with _file_lock(registry_path):
        projects = _load_registry_json()
        # Normalize: resolve worktrees → main repo, drop missing paths.
        projects = _normalize_registry(projects)
        repo_str = str(config.repo_root)
        if repo_str not in projects:
            projects.append(repo_str)
        registry_path.write_text(json.dumps(projects, indent=2))


def remove_from_projects_registry(path: str) -> None:
    """Remove a repo path from the global projects registry."""
    _ensure_state_dir()
    registry_path = STATE_DIR / "projects.json"
    with _file_lock(registry_path):
        projects = [p for p in _load_registry_json() if p != path]
        registry_path.write_text(json.dumps(projects, indent=2))


def load_projects_registry() -> list[str]:
    """Load list of known repo paths."""
    return _load_registry_json()


def load_and_reconcile(config: ResolvedConfig) -> AppState:
    """Load state, register project, reconcile worktrees, recover dead sessions.

    Saves state if any changes were made. Used by both TUI and fast mode startup.
    """
    state = load_state(config)
    update_projects_registry(config)
    changed = reconcile_state(state, config)
    changed = recover_dead_sessions(state) or changed
    if changed:
        save_state(state, config)
    return state
