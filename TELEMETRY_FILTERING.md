# Telemetry Event Filtering

## Problem

Previously, ALL Claude hook events were sent to the Aiceberg API, including:
- ❌ SessionEnd (cleanup)
- ❌ SessionStart (initialization)
- ❌ Setup (startup)
- ❌ Notification (status updates)
- ❌ WorktreeCreate/Remove (git operations)
- ❌ ConfigChange (configuration updates)

**Issues:**
1. **API cost overhead** - Wasted API calls for non-security events
2. **Dashboard clutter** - Telemetry events pollute the security dashboard
3. **No security value** - These events don't contribute to policy decisions

---

## Solution

**Smart event filtering**: Separate security-critical events from telemetry.

### Security-Critical Events (SENT to API)

| Hook | Event Type | Why Critical |
|------|-----------|--------------|
| `UserPromptSubmit` | `user_agt` | ✅ User input monitoring - can contain jailbreak attempts |
| `PreToolUse` | `agt_tool`/`agt_mem`/`agt_agt` | ✅ Tool execution control - prevent dangerous commands |
| `PostToolUse` | `agt_tool`/`agt_mem`/`agt_agt` | ✅ Tool output monitoring - detect data exfiltration |
| `PostToolUseFailure` | `agt_tool` | ✅ Tool error tracking - identify attack patterns |
| `PermissionRequest` | `agt_tool`/`agt_agt` | ✅ Permission mediation - enforce access control |
| `Stop` | `user_agt` + `agt_llm` | ✅ Final response + LLM turns - output safety |
| `SubagentStop` | `agt_agt` + `agt_llm` | ✅ Subagent LLM turns - nested agent safety |

---

### Telemetry-Only Events (LOGGED locally, NOT sent to API)

| Hook | Why Telemetry-Only |
|------|-------------------|
| `Setup` | Just initialization, no user content |
| `SessionStart` | Session metadata, no security relevance |
| `SessionEnd` | Cleanup signal, no actionable security data |
| `Notification` | Status updates, not user-driven |
| `TeammateIdle` | Idle tracking, no security impact |
| `TaskCompleted` | Task status, no content to evaluate |
| `ConfigChange` | Configuration updates, admin-controlled |
| `WorktreeCreate` | Git worktree creation, developer workflow |
| `WorktreeRemove` | Git worktree cleanup, developer workflow |
| `PreCompact` | Transcript compaction, internal housekeeping |

---

## Configuration

### Default Behavior (Recommended)

```bash
# .env
AICEBERG_SKIP_TELEMETRY_API_SEND="true"  # Default: skip telemetry
```

**Result:**
- ✅ Security events → Sent to Aiceberg API
- ✅ Telemetry events → Logged locally only
- ✅ Lower API costs
- ✅ Cleaner dashboard

---

### Send Everything (Not Recommended)

```bash
# .env
AICEBERG_SKIP_TELEMETRY_API_SEND="false"
```

**Use case:** Full audit trail for compliance/debugging

**Result:**
- ✅ All events → Sent to Aiceberg API
- ❌ Higher API costs
- ❌ Dashboard clutter

---

## How It Works

### Code Implementation

```python
# Define security-critical hooks
SECURITY_CRITICAL_HOOKS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Stop",
    "SubagentStop",
}

# Define telemetry-only hooks
TELEMETRY_ONLY_HOOKS = {
    "Setup",
    "SessionStart",
    "SessionEnd",
    "Notification",
    # ... etc
}

# In _one_shot_event():
skip_api = cfg.get("skip_telemetry_api_send", True) and hook_name in TELEMETRY_ONLY_HOOKS

if skip_api:
    # Log locally but don't send to API
    _append_local_log(cfg, create_payload, {"event_result": "telemetry_skipped"})
    return {"event_result": "passed", "telemetry_only": True}
```

---

## Verification

### Check Local Logs

```bash
# View telemetry events (logged but not sent)
cat logs/events.jsonl | jq 'select(.response.reason == "telemetry-only hook")'
```

**Example output:**
```json
{
  "timestamp": "2026-02-24T17:00:00Z",
  "payload": {
    "event_type": "agt_agt",
    "metadata": {
      "hook_event_name": "SessionEnd"
    }
  },
  "response": {
    "event_result": "telemetry_skipped",
    "reason": "telemetry-only hook"
  }
}
```

---

### Check Aiceberg Dashboard

**Before filtering:**
```
[Dashboard shows:]
- user_agt events
- agt_llm events
- agt_tool events
- SessionEnd events ❌ (clutter)
- Setup events ❌ (clutter)
- Notification events ❌ (clutter)
```

**After filtering:**
```
[Dashboard shows:]
- user_agt events ✅
- agt_llm events ✅
- agt_tool events ✅
(No telemetry clutter!)
```

---

## Comparison to Strands

### Strands (Native Hooks)

```python
# Strands only hooks security-relevant events
registry.add_callback(MessageAddedEvent, self.on_user_query)
registry.add_callback(BeforeToolCallEvent, self.on_tool_input)
registry.add_callback(AfterToolCallEvent, self.on_tool_output)
registry.add_callback(BeforeModelCallEvent, self.on_llm_input)
registry.add_callback(AfterModelCallEvent, self.on_llm_output)
registry.add_callback(AfterInvocationEvent, self.on_final_response)
```

**No SessionEnd, Setup, or Notification events exist in Strands!**

---

### Claude Hooks (Now Filtered)

```python
# Claude exposes many lifecycle hooks
# We filter to match Strands' security-focused approach

SECURITY_CRITICAL_HOOKS = {
    "UserPromptSubmit",    # → MessageAddedEvent
    "PreToolUse",          # → BeforeToolCallEvent
    "PostToolUse",         # → AfterToolCallEvent
    "Stop",                # → AfterInvocationEvent + LLM turns
    "SubagentStop",        # → Subagent LLM turns
}

TELEMETRY_ONLY_HOOKS = {
    "SessionEnd",          # (no Strands equivalent)
    "Setup",               # (no Strands equivalent)
    # ... etc
}
```

**Now we match Strands' security-focused model!** ✅

---

## API Cost Savings

### Example Session

**Without filtering:**
```
UserPromptSubmit → API call ✅
SessionStart → API call ❌ (wasted)
PreToolUse → API call ✅
PostToolUse → API call ✅
Stop → API call ✅
SessionEnd → API call ❌ (wasted)
Notification → API call ❌ (wasted)

Total: 7 API calls (3 wasted = 43% overhead)
```

**With filtering (AICEBERG_SKIP_TELEMETRY_API_SEND=true):**
```
UserPromptSubmit → API call ✅
SessionStart → local log only
PreToolUse → API call ✅
PostToolUse → API call ✅
Stop → API call ✅
SessionEnd → local log only
Notification → local log only

Total: 4 API calls (0 wasted = 0% overhead)
```

**Savings: 43% reduction in API calls!** 💰

---

## Migration Guide

### If You Want Cleaner Dashboard (Recommended)

**Already done!** Default is `AICEBERG_SKIP_TELEMETRY_API_SEND=true`

Just run:
```bash
python3 examples/single_query_demo.py
```

Check your dashboard - no more SessionEnd clutter!

---

### If You Need Full Audit Trail

Edit `.env`:
```bash
AICEBERG_SKIP_TELEMETRY_API_SEND="false"
```

All events will be sent to API.

---

## FAQ

### Q: Will I lose visibility into session lifecycle?

**A:** No! Telemetry events are still **logged locally** in `logs/events.jsonl`. You can:
```bash
# View all SessionEnd events
cat logs/events.jsonl | jq 'select(.payload.metadata.hook_event_name == "SessionEnd")'
```

---

### Q: Can I customize which events are telemetry-only?

**A:** Yes! Edit `TELEMETRY_ONLY_HOOKS` in `aiceberg_hooks_monitor.py`:

```python
TELEMETRY_ONLY_HOOKS = {
    "Setup",
    "SessionStart",
    "SessionEnd",
    # Remove events you want sent to API
    # Add events you want filtered
}
```

---

### Q: Does this affect blocking?

**A:** No! Blocking only happens on `BLOCK_CAPABLE_HOOKS`:
- UserPromptSubmit
- PreToolUse
- PostToolUse
- Stop
- SubagentStop

All security-critical events are **always sent to API** for policy evaluation.

---

## Summary

✅ **Default behavior:** Telemetry events logged locally, security events sent to API
✅ **Strands parity:** Matches Strands' security-focused approach
✅ **Cost savings:** 40-50% reduction in API calls
✅ **Dashboard clarity:** Only security-relevant events in Aiceberg UI
✅ **Full audit trail:** All events in local logs
✅ **Configurable:** Can be toggled via environment variable

**This is the correct, production-ready approach!** 🎉
