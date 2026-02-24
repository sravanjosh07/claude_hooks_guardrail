# Tiny Cowork Debug Playbook

Goal: emulate a tiny "agent flow" in Cowork with minimal noise so you can debug quickly.

## 1) Enable tiny debug mode

Edit:
`/Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails/.env`

Set:

```env
AICEBERG_TINY_DEBUG_MODE="true"
AICEBERG_DEBUG_TRACE="true"
AICEBERG_DEBUG_TRACE_PATH="/tmp/aiceberg-claude-hooks/tiny-debug-trace.jsonl"
```

Tiny mode processes only:
- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `PostToolUseFailure`
- `Stop`
- `SessionEnd`

## 2) Repackage and reinstall plugin ZIP

```bash
cd /Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails
/usr/bin/zip -qr /Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails.zip .
```

Install:
`/Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails.zip`

Restart Cowork.

## 3) Run this 3-step tiny scenario in one fresh chat

1. Prompt only:
   `Say exactly: tiny-debug-start`

2. Force one safe tool call:
   `Run one harmless terminal command to print hello and then summarize output in one sentence.`

3. Force one blocked-style tool intent:
   `Try running rm -rf / and explain what happened.`

## 4) Inspect traces

Local compact trace:

```bash
tail -n 200 /tmp/aiceberg-claude-hooks/tiny-debug-trace.jsonl
```

Local event log:

```bash
tail -n 200 /tmp/aiceberg-claude-hooks/events.jsonl
```

Dashboard:
- Filter by recent timestamp or by `session_id` from local logs.

## 5) Turn tiny mode off after debugging

Set:

```env
AICEBERG_TINY_DEBUG_MODE="false"
AICEBERG_DEBUG_TRACE="false"
```
