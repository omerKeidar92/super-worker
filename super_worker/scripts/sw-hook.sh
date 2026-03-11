#!/usr/bin/env bash
# Super Worker hook for Claude Code state detection.
# Called by Claude Code's hook system with the desired state as $1.
# Writes a state file so the TUI can detect changes via kqueue.
# Skips redundant writes to avoid ~19ms overhead per tool call.

set -euo pipefail

STATE="${1:-}"
if [ -z "$STATE" ] || [ -z "${SW_SESSION_NAME:-}" ]; then
    exit 0
fi

# Skip redundant state writes — state file is the source of truth.
# This avoids ~19ms of tmux subprocess overhead on every PreToolUse
# when Claude is already in "running" state.
STATE_DIR="${HOME}/.config/sw/session-states"
STATE_FILE="${STATE_DIR}/${SW_SESSION_NAME}"
if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE" 2>/dev/null)" = "$STATE" ]; then
    exit 0
fi

# Only set state if we're inside a tmux session managed by SW
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
        # TUI mode: per-session state via tmux env (legacy, kept for compatibility)
        tmux setenv -t "$SW_SESSION_NAME" SW_CC_STATE "$STATE"
    fi

    # Write state to file for kqueue-based detection (both modes)
    mkdir -p "$STATE_DIR"
    printf '%s' "$STATE" > "$STATE_FILE"
fi
