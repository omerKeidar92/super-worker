#!/usr/bin/env bash
# Real integration pilot — launches sw in tmux and captures actual screenshots.
# Usage: bash tests/real_pilot.sh
# Screenshots saved to /tmp/sw-real-pilot/
set -euo pipefail

SCREENSHOT_DIR="/tmp/sw-real-pilot"
SW_SESSION="sw-pilot-test"
STEP=0

rm -rf "$SCREENSHOT_DIR"
mkdir -p "$SCREENSHOT_DIR"

capture() {
    local name="$1"
    STEP=$((STEP + 1))
    local file="$SCREENSHOT_DIR/$(printf '%02d' $STEP)_${name}.txt"
    # Capture the full pane content with ANSI stripped
    tmux capture-pane -t "$SW_SESSION" -p > "$file" 2>/dev/null || true
    echo "📸 Step $STEP: $name → $file"
}

cleanup() {
    echo "🧹 Cleaning up..."
    tmux kill-session -t "$SW_SESSION" 2>/dev/null || true
}
trap cleanup EXIT

echo "═══════════════════════════════════════"
echo "  Real Integration Pilot Test"
echo "═══════════════════════════════════════"

# Kill any existing test session
tmux kill-session -t "$SW_SESSION" 2>/dev/null || true

# Start sw in a detached tmux session
echo "▶ Starting sw in tmux session '$SW_SESSION'..."
tmux new-session -d -s "$SW_SESSION" -x 140 -y 40 "cd $(pwd) && sw" 2>/dev/null

# Wait for app to initialize
echo "⏳ Waiting for app startup..."
sleep 3

capture "startup"

# Step 2: Press Ctrl+S to open new session dialog
echo "▶ Opening new session dialog (Ctrl+S)..."
tmux send-keys -t "$SW_SESSION" C-s
sleep 1
capture "new_session_dialog"

# Step 3: Press Enter to accept defaults (creates a claude session)
echo "▶ Creating session (Enter)..."
tmux send-keys -t "$SW_SESSION" Enter
sleep 2
capture "session_created"

# Step 4: Type some characters to test pending indicator
echo "▶ Typing 'hello' to test pending indicator..."
tmux send-keys -t "$SW_SESSION" h
sleep 0.1
capture "pending_after_h"

tmux send-keys -t "$SW_SESSION" e l l o
sleep 0.3
capture "pending_after_hello"

# Wait for pending to clear (500ms timeout)
sleep 1
capture "pending_cleared"

# Step 5: Create another session to test sidebar selection
echo "▶ Creating second session (Ctrl+S, Enter)..."
tmux send-keys -t "$SW_SESSION" C-s
sleep 1
capture "second_session_dialog"

tmux send-keys -t "$SW_SESSION" Enter
sleep 2
capture "second_session_created"

# Step 6: Check hook installation
echo "▶ Checking hook installation..."
if [ -f ~/.config/sw/sw-hook.sh ]; then
    echo "  ✅ Hook script exists at ~/.config/sw/sw-hook.sh"
    ls -la ~/.config/sw/sw-hook.sh > "$SCREENSHOT_DIR/hook_script_info.txt"
else
    echo "  ❌ Hook script NOT found at ~/.config/sw/sw-hook.sh"
fi

if [ -f ~/.claude/settings.json ]; then
    if grep -q "sw-hook.sh" ~/.claude/settings.json; then
        echo "  ✅ Hooks registered in ~/.claude/settings.json"
        grep -A2 "sw-hook" ~/.claude/settings.json > "$SCREENSHOT_DIR/hook_settings.txt" 2>/dev/null || true
    else
        echo "  ❌ Hooks NOT found in ~/.claude/settings.json"
    fi
fi

# Step 7: Check remain-on-exit on sessions
echo "▶ Checking remain-on-exit on tmux sessions..."
for sess in $(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^sw-'); do
    remain=$(tmux show-options -t "$sess" remain-on-exit 2>/dev/null | awk '{print $2}' || echo "unknown")
    echo "  Session $sess: remain-on-exit=$remain"
done > "$SCREENSHOT_DIR/remain_on_exit.txt" 2>&1

# Final screenshot
capture "final_state"

echo ""
echo "═══════════════════════════════════════"
echo "  Screenshots saved to $SCREENSHOT_DIR"
echo "═══════════════════════════════════════"
echo ""
echo "Files:"
ls -la "$SCREENSHOT_DIR/"
echo ""
echo "⏹ Done. Leaving session running for 5s before cleanup..."
sleep 5
