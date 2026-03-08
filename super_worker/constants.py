from pathlib import Path

STATE_DIR = Path.home() / ".config" / "sw"

TMUX_SESSION_PREFIX = "sw"
POLL_INTERVAL_MS = 200  # Legacy: used as fallback only
PANE_WATCHER_DEBOUNCE_MS = 16  # Debounce after kqueue signal
PANE_FALLBACK_POLL_S = 0.3  # Safety net poll interval
SIDEBAR_REFRESH_S = 5

DEFAULT_WORKTREE_NAME = "main"

RESERVED_KEYS = {"ctrl+n", "ctrl+s", "ctrl+a", "ctrl+t", "ctrl+r", "ctrl+d", "ctrl+e", "ctrl+q", "ctrl+o", "ctrl+shift+left", "ctrl+shift+right", "tab"}
