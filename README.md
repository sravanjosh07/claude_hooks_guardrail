# Aiceberg Claude Hooks Guardrails

Hooks-only guardrails plugin for Claude Cowork.

It intercepts Claude hook events, sends them to Aiceberg, and enforces pass/block decisions at block-capable hook boundaries.

## What You Get

- Observability for user prompts, tool calls, and session boundaries.
- Control to block risky prompts and tool calls before execution.
- Transcript-based `agt_llm` reconstruction at `Stop` / `SubagentStop`.

## Core Files

- `hooks/hooks.json`: hook wiring.
- `scripts/aiceberg_hooks_monitor.py`: hook entrypoint.
- `scripts/aiceberg_hooks/monitor.py`: event handling and policy decisions.
- `scripts/aiceberg_hooks/api.py`: payload send + response handling.
- `scripts/aiceberg_hooks/storage.py`: config, runtime state, transcript parsing.
- `.claude-plugin/plugin.json`: plugin manifest.
- `examples/single_query_demo.py`: terminal demo using the same monitor logic.

## Block-Capable Hooks

- `UserPromptSubmit`
- `PreToolUse`
- `PermissionRequest`
- `PostToolUse` (tool already ran; blocks downstream flow)
- `Stop`
- `SubagentStop`

## Quick Start

1. Copy env template and fill credentials.

```bash
cp .env.example .env
```

Set at least:
- `AICEBERG_API_KEY`
- `AICEBERG_PROFILE_ID`
- `AICEBERG_USE_CASE_ID`

2. Validate plugin.

```bash
claude plugin validate .
```

3. Run terminal demo (real send by default).

```bash
python3 examples/single_query_demo.py --safe-only
```

4. Build zip and install in Cowork.

```bash
zip -r ../aiceberg-claude-hooks-guardrails-local.zip . \
  -x ".git/*" ".venv/*" "*/__pycache__/*" "*.pyc" "logs/*"
```

5. In Cowork: Settings -> Plugins -> Install from ZIP.

## Runtime Notes

- Cowork UI may not show hook logs in chat; this is expected.
- Hooks run in background and still enforce decisions.
- Aiceberg dashboard is the primary observability surface.

Local debug sources:
- Claude session audit/debug logs under:
  - `~/Library/Application Support/Claude/local-agent-mode-sessions/`

## Runtime State Paths

The plugin auto-selects a writable runtime state directory.

Defaults resolve to a writable temp/home path and include:
- `events.jsonl`
- `monitor.db`
- `debug-trace.jsonl` (if enabled)

Optional override:
- `AICEBERG_STATE_DIR=/path/to/writable/dir`

## Optional Deep Dive

Detailed architecture and lifecycle diagrams:
- `docs/HOOKS_ONLY_ARCHITECTURE_DETAILED.html`
