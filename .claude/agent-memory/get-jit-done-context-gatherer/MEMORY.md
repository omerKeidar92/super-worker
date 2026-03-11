# Super Worker Fast Mode Design Context

## Project Architecture (Current)

### Core Structure
- **App Entry**: `SuperWorkerApp` (Textual TUI) in app.py
- **Per-Project**: `ProjectView` manages all worktrees for one repo
- **Per-Worktree**: `WorktreeTabContent` with sidebar + terminal preview
- **Session/Pane**: `TerminalPane` renders captured tmux pane output via Textual

### Key Components
- **Backend Services**: worktree.py, tmux.py, state.py (no TUI dependency)
- **State Management**: AppState → Worktrees → Sessions (pydantic models)
- **Terminal Rendering**: Captures tmux pane → renders via Textual Static widget
- **Output Watching**: PaneWatcher uses kqueue + tmux pipe-pane for efficiency

### Critical Files
- `/Users/omerkeidar/Projects/super-worker/super_worker/app.py` — Main TUI app
- `/Users/omerkeidar/Projects/super-worker/super_worker/widgets/project_view.py` — Worktree tabs, session mgmt
- `/Users/omerkeidar/Projects/super-worker/super_worker/widgets/terminal_pane.py` — Captured pane widget (TUI re-renders)
- `/Users/omerkeidar/Projects/super-worker/super_worker/services/tmux.py` — tmux session/pane management
- `/Users/omerkeidar/Projects/super-worker/super_worker/services/pane_watcher.py` — kqueue-based file watcher
- `/Users/omerkeidar/Projects/super-worker/super_worker/models.py` — Session, Worktree, AppState (core data)

## Lag Problem (Historical Context)

Recent commits show input lag was caused by:
1. Hash update AFTER scroll_end, triggering widget refresh
2. 500-line scrollback capture (reduced to 100)
3. Multiple refresh cycles per keystroke

Solutions applied:
- Moved hash update before try block (commit a3157b2)
- Throttled env-set per keystroke (throttle: 1.0s per session)
- kqueue file watcher instead of polling (commit a4e4ef9)
- Event-driven state detection

## Fast Mode Design Challenge

**Current approach** (TUI-based):
- tmux session runs in window
- TerminalPane captures pane output → Text.from_ansi() → Static widget renders
- Problem: Textual re-renders entire widget tree on every capture

**Proposed fast mode** (tmux split panes):
- No TUI rendering of pane content
- Use native tmux split panes to display each session
- Backend services (worktree, state, sessions) stay the same
- Sidebar/controls could be: TUI overlay, tmux native menu, or hybrid

## Key Constraints & Decisions

1. **Backend separation is clean** — tmux.py, worktree.py, state.py have no TUI imports
2. **Session model** is portable (Session has tmux_session_name + metadata)
3. **Worktree isolation** — each worktree has N sessions, each in separate tmux session
4. **Terminal pane watching** — kqueue avoids polling, but re-rendering is TUI problem
5. **Mouse/interaction** — TerminalPane forwards keys/pastes to tmux; need to design for native panes

## Design Implications for Fast Mode

- Cannot use TerminalPane widget (Textual widget)
- Need CLI-driven layout (tmux new-window, split-window, etc.) or hybrid TUI + direct attach
- Session selection needs tmux focus management instead of widget state
- State file persists across modes (compatibility maintained)
- Could keep sidebar as TUI overlay, panes as native tmux

## Implementation Status (Current Branch)

**Branch:** `feat/worktree-from-existing-branch`
- Fast mode core (fast_ui.py, fast_wizard.py, fast CLI commands) already implemented (commit ce4989f)
- Hook script already updated with per-pane state detection (SW_FAST_MODE env var)
- Models already have ui_mode field and tmux_pane_id
- Constants already have FAST_SESSION_PREFIX and FAST_STATUS_INTERVAL

**Performance Optimization Task:**
Focus is on reducing TUI rendering overhead via event-driven state detection:
1. Hook writes state to file (not just tmux env) for faster reads
2. Pane_watcher watches state files via kqueue, not just output pipes
3. Remove ContentChanged Message from terminal_pane per-render (was triggering full widget refresh)
4. Project_view._check_all_states → periodic state detection only (no per-render checks)
5. App periodic_refresh becomes state-file-driven instead of ContentChanged-driven

**State Files Location:** `~/.config/sw/state-{repo_hash}.json` (already implemented)
