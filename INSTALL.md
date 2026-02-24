# Install (Claude Cowork)

## 1. Configure env

```bash
cd aiceberg-claude-hooks-guardrails
cp .env.example .env
```

Edit `.env`:

```bash
AICEBERG_API_KEY="..."
AICEBERG_PROFILE_ID="..."
AICEBERG_USE_CASE_ID="..."
AICEBERG_API_URL="https://api.test1.aiceberg.ai/eap/v1/event"
AICEBERG_USER_ID="cowork_agent_yourname"

AICEBERG_ENABLED="true"
AICEBERG_MODE="enforce"
AICEBERG_DRY_RUN="false"
AICEBERG_PRINT_PAYLOADS="true"

# Optional toggles
AICEBERG_SKIP_TELEMETRY_API_SEND="true"
AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="true"
```

## 2. Validate

```bash
claude plugin validate .
```

## 3. Local sanity test (terminal)

```bash
python3 examples/single_query_demo.py --safe-only
```

Optional dry run:

```bash
python3 examples/single_query_demo.py --dry-run --safe-only
```

## 4. Build plugin zip

```bash
zip -r ../aiceberg-claude-hooks-guardrails-local.zip . \
  -x ".git/*" ".venv/*" "*/__pycache__/*" "*.pyc" "logs/*"
```

If you do not want `.env` inside zip, also add `".env"` to exclusions.

## 5. Install in Cowork

1. Open Claude Cowork.
2. Open Settings -> Plugins.
3. Click Install from ZIP.
4. Select `../aiceberg-claude-hooks-guardrails-local.zip`.
5. Enable the plugin.
6. Start a fresh Cowork session.

## 6. Verify

- Send a safe prompt.
- Send a tool-triggering prompt.
- Send a blocked prompt/tool request.

Expected:
- Events appear in Aiceberg dashboard.
- Blocked actions are denied in Cowork.

## 7. Reinstall after changes

After code edits:
1. Rebuild zip.
2. Reinstall zip in Cowork.
3. Start a new Cowork session.
