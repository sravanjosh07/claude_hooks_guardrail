# Code Refactoring Plan - aiceberg_hooks_monitor.py

## Current State Analysis

**File:** `scripts/aiceberg_hooks_monitor.py`
**Lines:** ~1534
**Complexity:** Medium-High

---

## Issues Identified

### 1. **Organization Issues**
- ❌ Constants scattered throughout file
- ❌ Helper functions mixed with main logic
- ❌ No clear section boundaries
- ❌ Related functions far apart

### 2. **Code Clarity Issues**
- ❌ Some functions doing too much (god functions)
- ❌ Unclear naming in places
- ❌ Comments explain WHAT but not WHY
- ❌ Magic strings/numbers

### 3. **Duplication Issues**
- ❌ Payload building logic repeated
- ❌ Metadata construction duplicated
- ❌ Similar error handling patterns

### 4. **Documentation Issues**
- ❌ Missing function-level WHY explanations
- ❌ No module-level overview
- ❌ Unclear flow for new readers

---

## Proposed Structure

### **New Organization:**

```python
#!/usr/bin/env python3
"""
Aiceberg Claude Hooks Monitor
==============================

Monitors Claude AI agent conversations for safety using Aiceberg API.

KEY CONCEPTS:
  - Every INPUT event must have exactly one OUTPUT event
  - Live events (user prompts, tools) sent to API immediately
  - Historical events (LLM turns from transcript) optional (configurable)
  - Telemetry events (SessionEnd, Setup) local-only by default

EVENT FLOW:
  1. UserPromptSubmit → user_agt INPUT
  2. PreToolUse → agt_tool INPUT
  3. PostToolUse → agt_tool OUTPUT
  4. Stop → Parse transcript, emit agt_llm + close user_agt
  5. SessionEnd → Cleanup (local-only by default)

BLOCKING:
  - If any INPUT is blocked, close all open events with policy message
  - Return {"decision": "block"} to Claude runtime
  - For PreToolUse: Return permissionDecision: deny
"""

# ============================================================================
# IMPORTS
# ============================================================================

# ============================================================================
# CONSTANTS - Event Types & Classification
# ============================================================================

# ============================================================================
# CONSTANTS - Configuration Defaults
# ============================================================================

# ============================================================================
# DATA STRUCTURES
# ============================================================================

# ============================================================================
# CONFIGURATION LOADING
# ============================================================================

# ============================================================================
# DATABASE - SQLite State Management
# ============================================================================

# ============================================================================
# AICEBERG API - Payload Building
# ============================================================================

# ============================================================================
# AICEBERG API - Network Communication
# ============================================================================

# ============================================================================
# TRANSCRIPT PARSING - LLM Turn Extraction
# ============================================================================

# ============================================================================
# EVENT HANDLERS - User Prompts
# ============================================================================

# ============================================================================
# EVENT HANDLERS - Tool Calls
# ============================================================================

# ============================================================================
# EVENT HANDLERS - LLM Turns
# ============================================================================

# ============================================================================
# EVENT HANDLERS - Session Lifecycle
# ============================================================================

# ============================================================================
# EVENT HANDLERS - Generic/Telemetry
# ============================================================================

# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

# ============================================================================
# CLI INTERFACE
# ============================================================================
```

---

## Specific Improvements

### 1. **Extract Common Patterns**

**BEFORE:**
```python
# Repeated in multiple places
metadata = _default_metadata(hook_name, data, user_id)
metadata.update({"tool_name": tool_name})
content = _normalize_text_payload(content_obj, int(cfg["max_content_chars"]))
create_payload = _build_create_payload(event_type, content, session_id, metadata, cfg)
```

**AFTER:**
```python
# Single helper for common pattern
def _create_event_payload(event_type, content, session_id, hook_name, data, user_id, cfg, **extra_meta):
    """Build event payload with standard metadata."""
    metadata = _default_metadata(hook_name, data, user_id)
    metadata.update(extra_meta)
    normalized = _normalize_text_payload(content, int(cfg["max_content_chars"]))
    return _build_create_payload(event_type, normalized, session_id, metadata, cfg)
```

---

### 2. **Better Function Naming**

**BEFORE:**
```python
def _emit_transcript_llm_turns(...)  # What does "emit" mean? Send to API? Log?
def _one_shot_event(...)             # What's "one_shot"?
def _handle_generic_hook_with_spec(...)  # Too generic
```

**AFTER:**
```python
def _process_llm_turns_from_transcript(...)  # Clear: reads transcript, processes turns
def _create_and_close_event(...)              # Clear: creates INPUT, sends OUTPUT immediately
def _handle_telemetry_event(...)              # Clear: non-security event
```

---

### 3. **Split God Functions**

**BEFORE:**
```python
def _emit_transcript_llm_turns(...):  # 60+ lines
    # Load transcript
    # Parse turns
    # Check cursor
    # Loop through turns
    # Build payloads
    # Send to API OR log locally
    # Handle blocks
    # Update cursor
```

**AFTER:**
```python
def _load_and_parse_transcript(transcript_path) -> list[tuple[str, str]]:
    """Load transcript file and extract LLM turns."""
    entries = _load_transcript_entries(transcript_path)
    return _extract_llm_turns(entries)

def _get_new_turns(conn, session_id, transcript_path, turns) -> list[tuple[int, str, str]]:
    """Get only new turns since last cursor position."""
    cursor_key = _transcript_cursor_key(session_id, transcript_path)
    last_idx = _get_transcript_cursor(conn, cursor_key)
    start_idx = max(0, last_idx + 1)
    return [(i, inp, out) for i, (inp, out) in enumerate(turns[start_idx:], start=start_idx)]

def _process_llm_turn(conn, cfg, idx, llm_input, llm_output, session_id, metadata, enforce, hook_name):
    """Process a single LLM turn - send to API or log locally based on config."""
    # Focused logic for one turn
```

---

### 4. **Add "WHY" Comments**

**BEFORE:**
```python
# Build payload
create_payload = _build_create_payload(...)
```

**AFTER:**
```python
# Build CREATE payload for INPUT event.
# Why: Aiceberg requires CREATE before UPDATE (paired event model).
create_payload = _build_create_payload(...)
```

---

### 5. **Constants Organization**

**BEFORE:**
```python
MAX_CONTENT_CHARS = 100000
DEFAULT_TIMEOUT = 15
# ... scattered throughout
TOOL_SUBAGENT_PATTERNS = (...)
MEM_PATTERNS = (...)
```

**AFTER:**
```python
# ============================================================================
# CONSTANTS - Event Classification
# ============================================================================

# Event types that can be blocked by policy
BLOCK_CAPABLE_HOOKS = {
    "UserPromptSubmit",
    "PreToolUse",
    # ... etc
}

# Security-critical hooks sent to API immediately
SECURITY_CRITICAL_HOOKS = {
    "UserPromptSubmit",
    "PreToolUse",
    # ... etc
}

# ============================================================================
# CONSTANTS - Content Limits & Timeouts
# ============================================================================

MAX_CONTENT_CHARS = 100000
DEFAULT_TIMEOUT_SECONDS = 15
STALE_EVENT_TTL_MINUTES = 30

# ============================================================================
# CONSTANTS - Tool Classification Patterns
# ============================================================================

TOOL_SUBAGENT_PATTERNS = ("task", "agent", "subagent")
MEMORY_TOOL_PATTERNS = ("memory", "store", "save")
```

---

### 6. **Better Error Handling**

**BEFORE:**
```python
try:
    response = requests.post(...)
    response.raise_for_status()
except Exception as e:
    print(f"Error: {e}")
    return {"event_result": "passed"}
```

**AFTER:**
```python
try:
    response = requests.post(...)
    response.raise_for_status()
except requests.Timeout:
    # Why fail-open: Don't block user if Aiceberg is slow/down
    _log_error(cfg, "Aiceberg API timeout - failing open", exc_info=True)
    return {"event_result": "passed"}
except requests.HTTPError as e:
    _log_error(cfg, f"Aiceberg API error: {e.response.status_code}", exc_info=True)
    return {"event_result": "passed"} if cfg.get("fail_open") else {"event_result": "rejected"}
```

---

### 7. **Type Hints Consistency**

**BEFORE:**
```python
def _send_payload(payload, cfg):  # Missing types
    ...
```

**AFTER:**
```python
def _send_payload(payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Send payload to Aiceberg API and return response."""
    ...
```

---

### 8. **Extract Magic Strings**

**BEFORE:**
```python
if result.get("event_result") in ("blocked", "rejected"):
    ...
if metadata.get("user_id") == "user":
    ...
```

**AFTER:**
```python
# Constants
BLOCKED_RESULTS = {"blocked", "rejected"}
PASSED_RESULTS = {"passed"}
USER_ID_USER = "user"
USER_ID_AGENT = "agent"

# Usage
if result.get("event_result") in BLOCKED_RESULTS:
    ...
if metadata.get("user_id") == USER_ID_USER:
    ...
```

---

## Refactoring Steps (Phased)

### Phase 1: Organization (No Logic Changes)
1. Add section headers with ASCII art
2. Group related functions
3. Move all constants to top
4. Alphabetize imports

### Phase 2: Documentation
1. Add module docstring
2. Add WHY comments to key functions
3. Document complex algorithms
4. Add examples to docstrings

### Phase 3: Extract Helpers
1. Extract common payload building
2. Extract common metadata building
3. Extract error handling patterns

### Phase 4: Simplify Complex Functions
1. Split `_emit_transcript_llm_turns` into smaller pieces
2. Simplify `handle_hook_event` dispatcher
3. Extract validation logic

### Phase 5: Polish
1. Consistent naming conventions
2. Remove dead code if any
3. Add type hints where missing
4. Final review

---

## Benefits After Refactoring

✅ **Readability:**
- Clear structure with section headers
- Related code grouped together
- "Why" comments explain intent

✅ **Maintainability:**
- Easy to find functions
- Changes localized to sections
- Less duplication = fewer bugs

✅ **Onboarding:**
- New developers can navigate easily
- Module docstring explains big picture
- Each section has clear purpose

✅ **Testing:**
- Smaller functions = easier to test
- Clear inputs/outputs
- Less mocking needed

✅ **Performance:**
- No impact (refactoring only)
- Same logic, better structure

---

## What NOT to Change

❌ **Don't change:**
1. External API (function signatures called from outside)
2. Event payloads sent to Aiceberg
3. Hook return format (Claude expects specific structure)
4. Database schema
5. Config file format
6. Log file format

✅ **Only change:**
1. Internal organization
2. Function names (internal only)
3. Code structure
4. Comments/documentation
5. Helper functions

---

## Estimated Impact

**Lines of code:** ~1534 → ~1400 (remove duplication)
**Functions:** ~40 → ~50 (more small functions, fewer large ones)
**Complexity:** Medium-High → Medium-Low
**Time to understand:** 2 hours → 30 minutes

---

## Validation

After refactoring, validate:
1. ✅ Run `single_query_demo.py` - all tests pass
2. ✅ Check event logs - same output
3. ✅ No new errors in stderr
4. ✅ Same API payloads sent
5. ✅ Same block behavior

---

## Next Steps

1. **Review this plan** - Get approval
2. **Create backup** - Copy original file
3. **Phase 1: Organization** - Group and section
4. **Phase 2: Documentation** - Add comments
5. **Phase 3-5: Refactor** - Improve code
6. **Validate** - Test thoroughly
7. **Commit** - Save clean version

Would you like me to proceed with the refactoring?
