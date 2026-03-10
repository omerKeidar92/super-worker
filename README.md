# Super Worker

TUI and CLI for managing multiple Claude Code sessions across git worktrees.

## Features

- **Multi-worktree management** — create isolated worktrees with their own Claude Code sessions
- **Live terminal preview** — see session output in real-time via kqueue + tmux pipe-pane
- **Full attach mode** — press Ctrl+A to drop into the tmux session directly
- **Session state indicators** — live dots show running, waiting for input, or waiting for approval
- **Attention alerts** — 🔔 appears on worktree tabs and project bar when a session needs approval
- **Multi-project support** — switch between repos with a project drawer or docked tab bar
- **Terminal sessions** — open plain shell sessions alongside Claude Code
- **Git operations** — commit, push, pull, and create PRs per worktree from the sidebar
- **Crash recovery** — dead sessions are automatically respawned on startup
- **Configurable** — per-project `.sw.toml` with auto-detection of remote, branch, and repo structure
- **CLI** — script worktree and session management from the command line

## Prerequisites

- **macOS** (uses kqueue for file watching and tmux)
- Python 3.11+
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`)

## Install

```bash
git clone https://github.com/okeidar/super-worker.git
cd super-worker
./setup.sh
```

Or manually with [pipx](https://pipx.pypa.io/):

```bash
git clone https://github.com/okeidar/super-worker.git
pipx install -e ./super-worker
```

This makes `sw` available globally from any terminal.

## Quick Start

Navigate to any git repository and run:

```bash
sw
```

From the TUI:

1. **Ctrl+N** — create a new worktree (with optional branch and initial prompt)
2. **Ctrl+S** — add a session to the current worktree
3. **Ctrl+A** — attach directly to the active tmux session
4. **Ctrl+T** — open a plain terminal session
5. Click sessions in the sidebar to switch between them

## Keybindings

| Key | Action |
|---|---|
| Ctrl+N | Create new worktree |
| Ctrl+S | Add session to current worktree |
| Ctrl+A | Attach to active tmux session |
| Ctrl+T | Open terminal session |
| Ctrl+R | Rename active session |
| Ctrl+D | Delete current worktree |
| Ctrl+O | Toggle project drawer |
| Ctrl+Shift+Left/Right | Switch between projects |
| Ctrl+E | Edit project settings |
| Ctrl+Q | Quit |
| x | Delete selected session (in sidebar) |

**In the project drawer:**

| Key | Action |
|---|---|
| p | Dock drawer as tab bar |
| Del | Remove project from registry |
| Esc | Close drawer |

## Worktree Management

When creating a worktree you can:

- **Create a new branch** (default) — auto-named with a configurable prefix
- **Use an existing branch** — check "Use existing branch" and provide the branch name
- **Detached HEAD** — create a worktree with no branch

When deleting a worktree, you can optionally delete the branch (both local and remote).

## Session Types

- **Claude Code** — runs `claude` with an optional prompt (e.g., `/plan`). Supports `--dangerously-skip-permissions` and `--continue` for resuming.
- **Terminal** — plain shell session for running commands, git operations, etc.

## Session States

Each session shows a colored dot indicating its state:

- 🟢 **Running** — session executing normally
- 🟡 **Waiting for input** — session blocked, waiting for user input
- 🟣 **Waiting for approval** — session needs user interaction
- 🔴 **Dead** — session exited (auto-recovered on startup)

When any session is waiting for approval, a 🔔 appears on the worktree tab and project bar.

## Git Actions

The sidebar provides per-worktree git operations:

- **Commit** — stages tracked changes and commits with a message
- **Push** — pushes to remote
- **Pull** — pulls from the main branch
- **Open PR** — creates a pull request via `gh pr create`

## Multi-Project Support

- **Ctrl+O** opens the project drawer to switch repos
- Projects can be docked as a tab bar above worktree tabs (press `p` in the drawer)
- Attention indicators (🔔) show across all open projects
- State persists per-project across sessions

## CLI

```bash
sw                                     # Launch TUI
sw new my-feature --prompt "/plan"     # Create worktree + session
sw add my-feature --prompt "/execute"  # Add session to existing worktree
sw list                                # List worktrees and sessions
sw cleanup my-feature                  # Kill sessions and remove worktree
sw config                              # Show current config
sw config worktree.branch_prefix sc-   # Set a config value
```

## Configuration

**No configuration is required.** Super Worker auto-detects your git remote, main branch, and repo structure.

To customize, use `Ctrl+E` in the TUI or the CLI:

```bash
sw config worktree.branch_prefix sc-
```

This creates a `.sw.toml` in your project root:

```toml
[worktree]
prefix = "repo-name"
branch_prefix = "sw-"
base_dir = "/path/to/worktrees"

[env]
symlinks = [".venv", ".claude"]
copies = [".env"]
post_create_hook = "scripts/setup.sh"

[git]
main_branch = "main"
remote = "origin"

[ui]
commit_placeholder = "Brief description of changes"
```

Global defaults go in `~/.config/sw/config.toml` (same format). Project settings override global.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
