"""Monkey-patches for Textual bugs. Imported early via __init__.py.

Remove individual patches as upstream fixes land.
"""

from textual import events as _events
from textual._xterm_parser import XTermParser as _XTermParser

# ---------------------------------------------------------------------------
# Textual drops the alt modifier for named keys (e.g. "enter", "tab") when
# processing legacy ESC+<key> sequences.  _sequence_to_key_events only
# applies "alt+" to single-character key names (len(name)==1).
#
# TODO: File upstream issue with Textual and replace this patch when fixed.
# ---------------------------------------------------------------------------
_original_seq_to_key = getattr(_XTermParser, "_sequence_to_key_events", None)

if callable(_original_seq_to_key):

    def _patched_seq_to_key(self, sequence, alt=False):
        for ev in _original_seq_to_key(self, sequence, alt=alt):
            if alt and "alt+" not in ev.key:
                yield _events.Key(f"alt+{ev.key}", ev.character)
            else:
                yield ev

    _XTermParser._sequence_to_key_events = _patched_seq_to_key
