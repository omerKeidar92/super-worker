#!/usr/bin/env bash
# Super Worker hook for Claude Code state detection.
# Called by Claude Code's hook system with the desired state as $1.
# Sets SW_CC_STATE on the tmux session so the TUI can detect Claude's state.

set -euo pipefail

STATE="${1:-}"
if [ -z "$STATE" ] || [ -z "${SW_SESSION_NAME:-}" ]; then
    exit 0
fi

# Only set state if we're inside a tmux session managed by SW
if command -v tmux &>/dev/null && tmux has-session -t "$SW_SESSION_NAME" 2>/dev/null; then
    tmux setenv -t "$SW_SESSION_NAME" SW_CC_STATE "$STATE"
fi
