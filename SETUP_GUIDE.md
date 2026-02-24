# Claude Hooks Plugin - Setup Guide

## Overview

This guide shows how to install and configure the Aiceberg guardrails plugin for Claude Cowork.

---

## Setup Steps

### 1. Plugin Location

Keep your plugin folder at:
```
/Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails
```

---

### 2. Configure Credentials

Put your Aiceberg credentials in:
```
/Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails/.env
```

**Required variables:**
```bash
AICEBERG_API_KEY="your-api-key"
AICEBERG_PROFILE_ID="your-profile-id"
AICEBERG_USE_CASE_ID="your-use-case-id"
AICEBERG_API_URL="https://api.test1.aiceberg.ai/eap/v1/event"
AICEBERG_USER_ID="cowork_agent_yourname"
```

You can reuse your existing MCP `.env` values for these credentials.

---

### 3. Install in Claude Cowork

**Create ZIP:**
```bash
cd /Users/sravanjosh/Documents/Aiceberg/Claude_agent_new
zip -r aiceberg-claude-hooks-guardrails.zip aiceberg-claude-hooks-guardrails \
  -x "*.git*" \
  -x "*/__pycache__/*" \
  -x "*/logs/*" \
  -x "*/.venv/*"
```

**Install:**
1. Open Claude Cowork
2. Go to Settings → Plugins
3. Click "Install from ZIP"
4. Select: `/Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails.zip`

---

### 4. Enable Plugin

1. In Claude Cowork settings, enable the `aiceberg-guardrails` plugin
2. Start a fresh session
3. Hooks will automatically start monitoring events

---

### 5. Hooks Configuration

Hooks are wired from:
```
/Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails/hooks/hooks.json
```

Once enabled, events start flowing automatically:
- `UserPromptSubmit` → user input monitoring
- `PreToolUse` → tool execution control
- `PostToolUse` → tool output monitoring
- `Stop` → final response + LLM turns
- `SessionEnd` → cleanup

---

## Event Filtering Configuration

### Default Behavior (Recommended)

The plugin has **smart event filtering** enabled by default:

```bash
# .env (already configured)
AICEBERG_SKIP_TELEMETRY_API_SEND="true"
AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="true"
```

**What gets sent to Aiceberg API:**
- ✅ `user_agt` - User prompts (security-critical)
- ✅ `agt_tool` - Tool calls (security-critical)
- ✅ `agt_mem` - Memory operations (security-critical)
- ❌ `agt_llm` - LLM turns (logged locally for now)
- ❌ `SessionEnd`, `Setup`, etc. - Telemetry (logged locally only)

**What gets logged locally:**
- ✅ **All events** are logged to `logs/events.jsonl`
- ✅ Full audit trail available for debugging
- ✅ No data loss, just selective API sending

---

## LLM Transcript Events - Phased Rollout

### Phase 1: Local-Only (Current)

```bash
# .env (default)
AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="true"
```

**Behavior:**
- `agt_llm` events from transcript reconstruction are **logged locally only**
- **Not sent to Aiceberg API**
- All other security events (user prompts, tools) **still sent normally**

**Why start local-only:**
1. LLM events are reconstructed from transcripts (not native hooks)
2. Test this feature separately before full rollout
3. Reduce initial API volume during testing
4. Focus dashboard on direct user/tool events first

**Local logs contain:**
```json
{
  "timestamp": "2026-02-24T17:00:00Z",
  "payload": {
    "event_type": "agt_llm",
    "input": "What is three added to four?",
    "output": "Three added to four equals 7.",
    "metadata": {
      "source": "transcript_turn"
    }
  },
  "response": {
    "event_result": "llm_local_only",
    "reason": "transcript reconstruction (local-only mode)"
  }
}
```

---

### Phase 2: Enable Live Sending (When Ready)

To enable sending `agt_llm` events to Aiceberg API:

```bash
# .env
AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="false"
```

**Behavior after change:**
- `agt_llm` events **sent to Aiceberg API**
- Same as all other security events
- Dashboard shows full LLM observability
- **No code changes needed** - just toggle the flag

**Use cases for live sending:**
1. Testing LLM output policies
2. Monitoring prompt injection in responses
3. Full conversation reconstruction
4. Advanced policy enforcement on LLM content

---

## Event Flow Summary

### With Defaults (AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY=true)

```
User: "What is 3 + 4?"
  ↓
[UserPromptSubmit] → user_agt INPUT → ✅ SENT TO API
  ↓
🤖 Claude processes internally
  ↓
[Stop] → Parse transcript:
        → agt_llm INPUT/OUTPUT → ❌ LOGGED LOCALLY ONLY
        → user_agt OUTPUT → ✅ SENT TO API
  ↓
[SessionEnd] → ❌ LOGGED LOCALLY ONLY (telemetry)
```

**API receives:**
- User prompt
- Final response
- (No LLM turns yet)

**Local logs contain:**
- User prompt
- LLM turns (reconstructed)
- Final response
- SessionEnd telemetry

---

### After Enabling (AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY=false)

```
User: "What is 3 + 4?"
  ↓
[UserPromptSubmit] → user_agt INPUT → ✅ SENT TO API
  ↓
🤖 Claude processes internally
  ↓
[Stop] → Parse transcript:
        → agt_llm INPUT/OUTPUT → ✅ SENT TO API
        → user_agt OUTPUT → ✅ SENT TO API
  ↓
[SessionEnd] → ❌ LOGGED LOCALLY ONLY (telemetry)
```

**API receives:**
- User prompt
- **LLM turns** ← New!
- Final response

---

## Verification

### Check Local Logs

```bash
# View all events
cat logs/events.jsonl | jq

# View local-only LLM events
cat logs/events.jsonl | jq 'select(.response.event_result == "llm_local_only")'

# View events sent to API
cat logs/events.jsonl | jq 'select(.response.event_id != null)'
```

---

### Check Aiceberg Dashboard

**With AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY=true (default):**
```
Dashboard shows:
- 👤 User prompts
- 🔧 Tool calls
- 💾 Memory operations
- (No LLM turns)
```

**After setting AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY=false:**
```
Dashboard shows:
- 👤 User prompts
- 💎 LLM turns ← New!
- 🔧 Tool calls
- 💾 Memory operations
```

---

## Rollback / Changes

### Disable LLM API Sending (Rollback)

```bash
# .env
AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="true"
```

Restart Claude session. LLM events will be local-only again.

---

### Send All Events (Full Audit Mode)

```bash
# .env
AICEBERG_SKIP_TELEMETRY_API_SEND="false"
AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="false"
```

**Result:**
- ✅ User prompts → API
- ✅ LLM turns → API
- ✅ Tool calls → API
- ✅ Telemetry (SessionEnd, etc.) → API
- ✅ Full dashboard visibility
- ❌ Higher API costs

---

## Troubleshooting

### Plugin Not Loading

```bash
# Check if ZIP exists
ls -lh /Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails.zip

# Check Claude plugins directory
ls ~/.claude/plugins/
```

---

### Hooks Not Firing

1. Check plugin is enabled in Claude Cowork settings
2. Start a fresh session (hooks activate on session start)
3. Check logs:
   ```bash
   tail -f logs/events.jsonl
   ```

---

### No Events in Dashboard

**Check `.env`:**
```bash
# Should be:
AICEBERG_ENABLED="true"
AICEBERG_MODE="enforce"
AICEBERG_DRY_RUN="false"  # Or not set (defaults to false)
```

**Check credentials:**
```bash
grep AICEBERG_API_KEY .env
grep AICEBERG_PROFILE_ID .env
```

---

### LLM Events Not Appearing

**Expected behavior with default settings:**
- LLM events are **local-only** by default
- Check local logs: `cat logs/events.jsonl | jq 'select(.payload.event_type == "agt_llm")'`
- To send to API: Set `AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="false"`

---

## Migration from MCP

If you're migrating from the MCP-based monitor:

### 1. Copy Credentials

```bash
# Copy your existing .env from MCP setup
cp /path/to/old/mcp/.env /Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails/.env
```

### 2. Add New Flags

Add to `.env`:
```bash
AICEBERG_SKIP_TELEMETRY_API_SEND="true"
AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="true"
```

### 3. Test

```bash
# Test with Python demo first
cd /Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails
python3 examples/single_query_demo.py
```

### 4. Install in Cowork

Follow steps 3-4 above to install the plugin.

---

## Summary

✅ **Setup Steps:**
1. Keep plugin at specified path
2. Configure `.env` with Aiceberg credentials
3. Create and install ZIP in Claude Cowork
4. Enable plugin in settings
5. Start fresh session

✅ **Default Behavior:**
- User prompts → API ✅
- Tool calls → API ✅
- LLM turns → Local logs only ❌ (for initial testing)
- Telemetry → Local logs only ❌ (cost optimization)

✅ **No Testing Required:**
- Just setup and configuration
- Hooks activate automatically
- Events flow once plugin is enabled

✅ **Future Enablement:**
- Set `AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="false"` when ready
- No code changes needed
- Restart session to apply

**Plugin is ready for deployment!** 🚀
