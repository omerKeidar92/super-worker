# Performance Fixes Context — 4 Targeted Optimizations

**Date Created:** March 11, 2026
**Branch:** feat/worktree-from-existing-branch
**Related Commits:** 75d8baa (Event-driven state), 0773522 (Input lag fix)

---

## Overview

Four focused performance optimizations to remove dead code and tighten constants that were introduced during the event-driven state detection refactor (commit 75d8baa).

All changes are in the TUI rendering pipeline (NOT fast mode). They reduce subprocess calls, cleanup throttling code no longer needed, and optimize polling fallbacks.

---

## Fix 1: Remove `_set_session_env` from `send_keys` in `tmux.py`

### Current State

File: `/Users/omerkeidar/Projects/super-worker/super_worker/services/tmux.py`

Lines 222–239 (send_keys function):
- Calls `_set_session_env(tmux_session_name, "SW_CC_STATE", "running")` with 1.0s throttle
- This was needed when state detection relied on reading tmux env vars
- **Now dead code** — state detection switched to reading state files (commit 75d8baa)

Lines 129–136 (_set_session_env function):
- Sets a tmux session environment variable
- Only used by send_keys and test_tmux.py
- Can be fully removed after send_keys cleanup

### Why It's Dead

1. **Commit 75d8baa** introduced file-based state: hook writes to `~/.config/sw/session-states/{session_name}`
2. **PaneWatcher** watches state files via kqueue (instant, event-driven)
3. **on_terminal_pane_state_changed** in project_view.py reads state files with `read_state_file()`
4. Setting tmux env vars is no longer part of the detection chain

### What to Do

1. Remove lines 231–235 from `send_keys()` (the throttled `_set_session_env` call)
2. Remove entire `_set_session_env()` function (lines 129–136)
3. Remove the throttle state var `_last_state_set` and `_STATE_SET_THROTTLE_S` (lines 218–219)
4. Update test_tmux.py: remove or mark `TestSetSessionEnv` as testing removed code

### Tests Affected

File: `/Users/omerkeidar/Projects/super-worker/tests/test_tmux.py`
- Class `TestSetSessionEnv` (lines 184–194) — tests `_set_session_env` directly
- Remove this test class (function being removed)

---

## Fix 2: Change `PANE_FALLBACK_POLL_S` from 0.3s to ~5s in `constants.py`

### Current State

File: `/Users/omerkeidar/Projects/super-worker/super_worker/constants.py`

Line 9:
```python
PANE_FALLBACK_POLL_S = 0.3  # Safety net poll interval
```

### Why It Should Change

1. **kqueue handles real-time detection**: PaneWatcher watches state files via kqueue (instant notifications)
2. **0.3s fallback is too aggressive**: Creates 3-4 polls per second if kqueue fails
3. **Long polling is a safety net, not primary**: Should only activate if kqueue breaks
4. **~5s aligns with periodic_refresh**: Other long-running operations poll every 5s

### Risk Assessment

- **kqueue is OS-level** (available on all macOS/BSD) — very reliable
- Fallback should trigger only if file watcher fails catastrophically
- 5s is still responsive for the rare case kqueue doesn't work

### Tests Affected

No direct tests of this constant in test_perf_event_driven.py or test_tmux.py.

---

## Fix 3: Replace `batch_detect_session_states` in `periodic_refresh` with Lightweight Check

### Current State

File: `/Users/omerkeidar/Projects/super-worker/super_worker/widgets/project_view.py`

Lines 601–620 (periodic_refresh method):
```python
async def periodic_refresh(self) -> None:
    # Full batch detect to catch dead sessions
    all_session_names = [s.tmux_session_name for wt in self._state.worktrees for s in wt.sessions]
    if all_session_names:
        full_states = await asyncio.to_thread(batch_detect_session_states, all_session_names)
        old_attention = has_waiting_approval(self._cached_session_states)
        self._cached_session_states.update(full_states)
        new_attention = has_waiting_approval(self._cached_session_states)
        # ... check attention change
```

**What this does:**
- Calls `batch_detect_session_states()` every 5s
- For each session: checks if pane is dead (`pane_dead == "1"`) + reads tmux env var
- Total: N subprocess calls + libtmux API calls every 5 seconds

### The Refactoring

**The challenge:** State files can't detect pane death (they're written by hook only while Claude runs)

**What we need to keep:**
1. Dead session detection (call `is_session_alive()` for each session)
2. State cache sync for terminal sessions (they don't have state files, only hook writes)

**New approach:**
1. Call lightweight `is_session_alive()` for dead detection (checks `pane_dead` flag only, ~1ms per session)
2. Call `read_all_state_files()` to sync state cache (pure file reads, ~0.1ms per session)
3. Skip env var reading entirely (state files are the source of truth now)

### Implementation Details

**Functions involved:**
- `batch_detect_session_states()` in tmux.py (lines 276–312) — DO NOT remove, used elsewhere
- `is_session_alive()` in tmux.py (lines 241–248) — lightweight alive check only
- `read_all_state_files()` in tmux.py (lines 84–100) — read state files, no subprocess

**New periodic_refresh logic:**
```python
# Detect dead sessions (rare, but needed)
alive_states = {}
for name in all_session_names:
    if is_session_alive(name):
        # Read state file for alive sessions
        alive_states[name] = read_state_file(name)
    else:
        alive_states[name] = SessionState.DEAD

# Update cache and check attention
self._cached_session_states.update(alive_states)
```

### Why This Works

1. **Dead detection is reliable**: `pane_dead` flag set by tmux when process exits
2. **State file reading is fast**: ~0.1ms per session (file read, no subprocess)
3. **Terminal sessions**: If they don't have state files, `read_state_file()` returns UNKNOWN (safe fallback)
4. **Maintains correctness**: Still detects dead sessions, updates state cache

### Tests Affected

File: `/Users/omerkeidar/Projects/super-worker/tests/test_perf_event_driven.py`
- Tests verify: periodic_refresh still detects dead sessions, state cache syncs correctly
- May need to verify: `is_session_alive()` returns DEAD when pane_dead == "1"

File: `/Users/omerkeidar/Projects/super-worker/tests/test_tmux.py`
- Class `TestBatchDetectDeadPanes` (lines 217–231) — still relevant (tests pane_dead detection)
- Tests for `is_session_alive()` (lines 197–214) — keep these

---

## Fix 4: Add `exclusive=True` to send-keys workers in `terminal_pane.py`

### Current State

File: `/Users/omerkeidar/Projects/super-worker/super_worker/widgets/terminal_pane.py`

Lines 213–222 (_send_keys_async method):
```python
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
```

**Missing:** `exclusive=True` parameter

### Why It Should Be Added

1. **send_keys sends keystrokes to tmux**: Multiple rapid keypresses could queue as concurrent workers
2. **exclusive=True ensures serialization**: Only one send_keys worker runs at a time
3. **Textual worker groups**: `group="send-keys"` allows grouped workers, but `exclusive=True` makes the group exclusive
4. **Prevents tmux race conditions**: Send keys one at a time, not in parallel

### Risk Assessment

- **No risk**: Exclusive workers are standard Textual pattern
- **Already done for _capture**: Line 161 in _poll_pane() uses `exclusive=True`
- **Consistency**: Matches the pane-capture worker pattern

### Tests Affected

No direct test of this parameter (it's a Textual runtime behavior).

---

## Summary Table

| Fix | File | Lines | Change | Removes | Impact |
|-----|------|-------|--------|---------|--------|
| 1 | tmux.py | 129–136, 218–219, 231–235 | Remove `_set_session_env` and throttle | 10 lines | Dead code cleanup |
| 2 | constants.py | 9 | 0.3 → 5 seconds | N/A | Tuning only |
| 3 | project_view.py | 610–620 | Replace `batch_detect_session_states` | N/A | Logic refactor |
| 4 | terminal_pane.py | 222 | Add `exclusive=True` | N/A | Single-line addition |

---

## Testing Strategy

### Before Changes
- Run `pytest tests/test_perf_event_driven.py tests/test_tmux.py -v`
- Verify all tests pass

### After Changes

**Fix 1 (remove _set_session_env):**
- Remove `TestSetSessionEnv` test class
- Run tests: should pass (no longer called)
- Verify send_keys still works via manual testing

**Fix 2 (PANE_FALLBACK_POLL_S):**
- No test changes needed
- The constant is only used in terminal_pane.py:110 for set_interval()
- Verify in manual test: still polls if kqueue stalls

**Fix 3 (periodic_refresh refactor):**
- Update tests if they mock `batch_detect_session_states`
- Verify: dead sessions still detected
- Verify: state cache updated correctly
- Verify: attention indicator still works

**Fix 4 (exclusive=True):**
- No test changes needed
- Verify in manual test: rapid keypresses don't race

---

## Key Files Reference

- **tmux.py**: `/Users/omerkeidar/Projects/super-worker/super_worker/services/tmux.py`
- **constants.py**: `/Users/omerkeidar/Projects/super-worker/super_worker/constants.py`
- **terminal_pane.py**: `/Users/omerkeidar/Projects/super-worker/super_worker/widgets/terminal_pane.py`
- **project_view.py**: `/Users/omerkeidar/Projects/super-worker/super_worker/widgets/project_view.py`
- **test_perf_event_driven.py**: `/Users/omerkeidar/Projects/super-worker/tests/test_perf_event_driven.py`
- **test_tmux.py**: `/Users/omerkeidar/Projects/super-worker/tests/test_tmux.py`

---

## Dependency Order

**Recommended implementation order:**
1. Fix 4 (exclusive=True) — simplest, no interdependencies
2. Fix 1 (remove _set_session_env) — cleanup, straightforward
3. Fix 2 (PANE_FALLBACK_POLL_S) — constant change, isolated
4. Fix 3 (periodic_refresh refactor) — most complex, depends on understanding state detection

---

## Architecture Context

### State Detection Flow (Post-Event-Driven Refactor)

1. **Hook writes state file**: sw-hook.sh → `~/.config/sw/session-states/{session_name}`
2. **PaneWatcher watches files**: kqueue detects state file change
3. **PaneWatcher calls callback**: `on_state_changed()` posted to message bus
4. **TerminalPane posts StateChanged**: TUI receives instant notification
5. **ProjectView.on_terminal_pane_state_changed**: Reads state file (pure file I/O) and updates UI

**periodic_refresh still needed for:**
- Git status polling (no event source)
- Dead session detection (state files can't detect pane death)
- Terminal session state syncing (no hook, no state file)

---

## Related Documentation

- **Fast mode architecture**: `codegen/fast-mode.md` (discusses state file design)
- **Event-driven state commit**: `75d8baa` ("Event-driven state detection and pipe file truncation")
- **Performance test coverage**: `tests/test_perf_event_driven.py` (22 tests)
