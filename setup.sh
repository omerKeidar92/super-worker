#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $*"; }
warn()  { echo -e "${YELLOW}!${NC} $*"; }
fail()  { echo -e "${RED}✗${NC} $*"; exit 1; }

echo "Super Worker — Setup"
echo "===================="
echo

# ── Python ──────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    fail "Python 3 not found. Install Python 3.11+ first."
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYMAJOR=$(echo "$PYVER" | cut -d. -f1)
PYMINOR=$(echo "$PYVER" | cut -d. -f2)

if [ "$PYMAJOR" -lt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -lt 11 ]; }; then
    fail "Python $PYVER found, but 3.11+ is required."
fi
info "Python $PYVER"

# ── tmux ────────────────────────────────────────────────────────────────
if ! command -v tmux &>/dev/null; then
    warn "tmux not found — installing..."
    if command -v brew &>/dev/null; then
        brew install tmux
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq tmux
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y tmux
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm tmux
    else
        fail "Cannot auto-install tmux. Install manually: https://github.com/tmux/tmux/wiki/Installing"
    fi
    info "tmux installed ($(tmux -V))"
else
    info "tmux $(tmux -V | awk '{print $2}')"
fi

# ── Claude Code CLI ─────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
    warn "Claude Code CLI not found."
    echo "  Install via: npm install -g @anthropic-ai/claude-code"
    echo "  See: https://docs.anthropic.com/en/docs/claude-code"
    echo
fi

# ── pipx ───────────────────────────────────────────────────────────────
if ! command -v pipx &>/dev/null; then
    warn "pipx not found — installing..."
    if command -v brew &>/dev/null; then
        brew install pipx
    else
        python3 -m pip install --user pipx
    fi
    pipx ensurepath 2>/dev/null || true
    info "pipx installed"
else
    info "pipx"
fi

# ── Install super-worker ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo
echo "Installing super-worker globally via pipx..."
if pipx list 2>/dev/null | grep -q "super-worker"; then
    pipx install -e "$SCRIPT_DIR" --force 2>&1 | tail -1
else
    pipx install -e "$SCRIPT_DIR" 2>&1 | tail -1
fi
info "super-worker installed (available globally as 'sw')"

echo
echo "Done! Run 'sw' from any terminal to start."
