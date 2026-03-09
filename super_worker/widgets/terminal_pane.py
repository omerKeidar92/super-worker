import logging
import re

from rich.text import Text
from textual.events import Click, Key, Paste
from textual.reactive import reactive
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from super_worker.constants import PANE_FALLBACK_POLL_S, RESERVED_KEYS
from super_worker.services.pane_watcher import PaneWatcher
from super_worker.services.tmux import capture_pane, send_keys

logger = logging.getLogger(__name__)

# Strip ANSI background color sequences to avoid theme bleed
_BG_ANSI_RE = re.compile(r"\x1b\[(?:4[0-9]|10[0-7]|48;[0-9;]*)m")


class TerminalPane(Widget, can_focus=True):
    """Displays captured tmux pane content and forwards keystrokes.

    This is a preview — for full interaction (cursor, scrolling, CC UI),
    press Ctrl+A to attach directly to the tmux session.
    """

    active_session: reactive[str | None] = reactive(None)

    DEFAULT_CSS = """
    TerminalPane {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
        layout: vertical;
    }
    TerminalPane:focus {
        border: tall $accent;
    }
    #terminal-scroll {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
    }
    #terminal-content {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_hash: int = 0
        self._fallback_timer = None
        self._watcher = PaneWatcher()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="terminal-scroll"):
            content = Static("Select a session · Ctrl+A to attach", id="terminal-content")
            content.auto_links = False
            content.ALLOW_SELECT = False
            yield content

    def watch_active_session(self, old_value: str | None, session_name: str | None) -> None:
        if old_value:
            self._watcher.stop_watching(old_value)
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None
        self._last_hash = 0
        try:
            content = self.query_one("#terminal-content", Static)
            if session_name:
                content.update("")
            else:
                content.update("Select a session · Ctrl+A to attach")
        except Exception:
            logger.debug("terminal-content widget not available during session switch", exc_info=True)
        if session_name:
            self._watcher.start_watching(session_name, self._on_pane_output)
            self._poll_pane()  # Initial capture
            self._fallback_timer = self.set_interval(PANE_FALLBACK_POLL_S, self._poll_pane)

    def _on_pane_output(self) -> None:
        """Called by PaneWatcher when pipe-pane file has new data."""
        try:
            self.app.call_later(self._poll_pane)
        except Exception:
            pass

    def _poll_pane(self) -> None:
        session = self.active_session
        if not session:
            return
        self.run_worker(lambda: self._capture(session), thread=True, exclusive=True)

    def _capture(self, session_name: str) -> tuple[int, Text] | None:
        raw = capture_pane(session_name)
        content_hash = hash(raw)
        if content_hash == self._last_hash:
            return None
        clean = _BG_ANSI_RE.sub("", raw)
        return content_hash, Text.from_ansi(clean)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state != WorkerState.SUCCESS or event.worker.result is None:
            return
        try:
            self.query_one("#terminal-content", Static).update(event.worker.result[1])
            # Update hash right after content renders so duplicate captures
            # are skipped; scroll_end failure must not prevent this.
            self._last_hash = event.worker.result[0]
            self.query_one("#terminal-scroll", VerticalScroll).scroll_end(animate=False)
        except Exception:
            logger.debug("terminal-content widget not available during pane update", exc_info=True)

    # Map Textual key names to tmux special key names
    _SPECIAL_KEY_MAP = {
        "enter": "Enter",
        "return": "Enter",
        "escape": "Escape",
        "backspace": "BSpace",
        "delete": "DC",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "home": "Home",
        "end": "End",
        "pageup": "PPage",
        "pagedown": "NPage",
    }

    # Key combos that insert a newline in Claude Code's input.
    _NEWLINE_KEYS = {
        "shift+enter", "shift+return",
        "alt+enter", "alt+return",
    }

    def on_unmount(self) -> None:
        if self._fallback_timer is not None:
            self._fallback_timer.stop()
            self._fallback_timer = None
        self._watcher.cleanup()

    def _send_keys_async(self, *keys: str, literal: bool = False) -> None:
        """Send keys off the event loop. kqueue watcher handles rendering."""
        session = self.active_session
        if not session:
            return
        self.run_worker(
            lambda: send_keys(session, *keys, literal=literal),
            thread=True,
            group="send-keys",
        )

    def on_click(self, event: Click) -> None:
        """Consume clicks so the Static child doesn't trigger text selection."""
        event.stop()
        self.focus()

    def on_paste(self, event: Paste) -> None:
        if not self.active_session or not event.text:
            return
        event.stop()
        self._send_keys_async(event.text, literal=True)

    def on_key(self, event: Key) -> None:
        if not self.active_session:
            return

        key = event.key
        if key in RESERVED_KEYS:
            return

        event.prevent_default()
        event.stop()

        if key in self._NEWLINE_KEYS:
            # Forward as Alt+Enter (ESC followed by Enter) so Claude Code
            # interprets it as "insert newline" rather than "submit".
            self._send_keys_async("Escape", "Enter")
        elif key in self._SPECIAL_KEY_MAP:
            self._send_keys_async(self._SPECIAL_KEY_MAP[key])
        elif event.character and len(event.character) == 1:
            # Send printable characters as literal text so that '/', ';',
            # and other tmux-special characters arrive unmangled.
            self._send_keys_async(event.character, literal=True)
        elif key.startswith("ctrl+"):
            letter = key.split("+", 1)[1]
            self._send_keys_async(f"C-{letter}")
