#!/usr/bin/env bash
# Super Worker hook for Claude Code state detection.
# Called by Claude Code's hook system with the desired state as $1.
# Sets SW_CC_STATE on the tmux session and writes a state file so the TUI
# can detect changes via kqueue without polling subprocess calls.

set -euo pipefail

STATE="${1:-}"
if [ -z "$STATE" ] || [ -z "${SW_SESSION_NAME:-}" ]; then
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
    STATE_DIR="${HOME}/.config/sw/session-states"
    mkdir -p "$STATE_DIR"
    printf '%s' "$STATE" > "${STATE_DIR}/${SW_SESSION_NAME}"
fi
