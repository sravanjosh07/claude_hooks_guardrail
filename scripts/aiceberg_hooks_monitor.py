#!/usr/bin/env python3
"""
Aiceberg Claude Hooks Monitor
===============================================================================

Monitors Claude AI agent conversations for safety using Aiceberg API.

ARCHITECTURE:
  - Each hook invocation runs as a separate subprocess
  - SQLite database maintains state across invocations
  - Every INPUT event must have exactly one OUTPUT event
  - Block decisions returned as JSON to Claude runtime

EVENT CLASSIFICATION:
  - Security-Critical: Sent to Aiceberg API immediately (user prompts, tools)
  - Telemetry-Only: Logged locally, not sent to API (SessionEnd, Setup, etc.)
  - LLM Transcript: Configurable (reconstructed from history at Stop hook)

KEY CONCEPTS:

  INPUT/OUTPUT Pairing:
    - CREATE event → INPUT content, get event_id
    - UPDATE event → OUTPUT content, linked to event_id
    - Why: Aiceberg requires paired events for conversation flow

  Live vs Historical Events:
    - Live: UserPromptSubmit, PreToolUse (can block before execution)
    - Historical: LLM turns from transcript (read after LLM already ran)
    - Why: Claude doesn't expose BeforeModelCallEvent/AfterModelCallEvent

  Blocking Flow:
    - If INPUT blocked → close all open events with policy message
    - Return {"decision": "block"} to Claude runtime
    - For tools: Return {"permissionDecision": "deny"}
    - Why: Ensures clean state even when execution stops mid-flow

EVENT FLOW EXAMPLE:

  User: "What is 3 + 4?"
    ↓
  [UserPromptSubmit] → user_agt INPUT (LIVE - can block)
    ↓
  Claude processes internally (LLM calls - NOT HOOKED)
    ↓
  [Stop] → Read transcript, emit agt_llm turns (HISTORICAL)
         → Close user_agt OUTPUT
    ↓
  [SessionEnd] → Cleanup (local-only by default)

CONFIGURATION:
  AICEBERG_SKIP_TELEMETRY_API_SEND="true"    # Don't send SessionEnd, etc. to API
  AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY="true"  # Don't send LLM turns to API (yet)

This script is invoked once per hook event and reads hook payload JSON on stdin.
"""

# ============================================================================
# IMPORTS
# ============================================================================

import argparse
import json
import os
import sqlite3
import ssl
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


# ============================================================================
# CONSTANTS - Version & Paths
# ============================================================================

VERSION = "1.0.0"
DEFAULT_DB_PATH = "/tmp/aiceberg-claude-hooks/monitor.db"
DEFAULT_LOG_PATH = "/tmp/aiceberg-claude-hooks/events.jsonl"
MAX_CONTENT_CHARS = 50000  # Maximum characters to send in event payload
OPEN_EVENT_TTL_SECONDS = 1800  # 30 minutes - cleanup stale events

# ============================================================================
# CONSTANTS - Security & Redaction
# ============================================================================

# Keys to redact from payloads before sending to API
# Why: Prevent accidental credential leakage in event logs
REDACT_KEYS = {
    "api_key",
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
}

# ============================================================================
# CONSTANTS - Tool Classification Patterns
# ============================================================================

# Patterns for memory/storage tool detection
# Why: Memory tools get classified as "agt_mem" instead of "agt_tool"
MEM_PATTERNS = ("memory", "store", "save", "remember", "retrieve")

# ============================================================================
# CONSTANTS - Hook Event Classification
# ============================================================================

# Hook events that support explicit block decisions
# Why: Only these hooks can return {"decision": "block"} to Claude
BLOCK_CAPABLE_HOOKS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "SubagentStop",
    "TeammateIdle",
    "TaskCompleted",
    "ConfigChange",
}

# Security-critical hooks sent to Aiceberg API immediately
# Why: These events have real-time security value for blocking/monitoring
SECURITY_CRITICAL_HOOKS = {
    "UserPromptSubmit",    # User input monitoring (can block jailbreak attempts)
    "PreToolUse",          # Tool execution control (can block dangerous commands)
    "PostToolUse",         # Tool output monitoring (can detect data exfiltration)
    "PostToolUseFailure",  # Tool error tracking (can identify attack patterns)
    "PermissionRequest",   # Permission mediation (access control)
    "Stop",                # Final response + LLM turns (output safety)
    "SubagentStop",        # Subagent LLM turns (nested agent safety)
}

# Telemetry-only hooks (logged locally, not sent to API by default)
# Why: These are lifecycle events with no security-critical content
TELEMETRY_ONLY_HOOKS = {
    "Setup",           # Initialization (no user content)
    "SessionStart",    # Session metadata (no security relevance)
    "SessionEnd",      # Cleanup signal (no actionable security data)
    "Notification",    # Status updates (not user-driven)
    "TeammateIdle",    # Idle tracking (no security impact)
    "TaskCompleted",   # Task status (no content to evaluate)
    "ConfigChange",    # Configuration updates (admin-controlled)
    "WorktreeCreate",  # Git worktree creation (developer workflow)
    "WorktreeRemove",  # Git worktree cleanup (developer workflow)
    "PreCompact",      # Transcript compaction (internal housekeeping)
}

# Tiny debug mode: Only core flow hooks (reduces log noise)
# Why: For quick testing without overwhelming output
TINY_DEBUG_HOOKS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SessionEnd",
}


# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass(frozen=True)
class HookEventEnvelope:
    """Wrapper for incoming hook event data from Claude."""
    hook_name: str
    session_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class OpenEventRecord:
    """
    Represents an INPUT event awaiting its OUTPUT.

    Why: Stored in SQLite to track open events across subprocess invocations.
    Every INPUT must eventually get an OUTPUT (or be closed on error).
    """
    event_id: str        # Aiceberg event ID from CREATE response
    event_type: str      # user_agt, agt_llm, agt_tool, etc.
    session_id: str      # Claude session ID
    input_content: str   # Original INPUT content
    metadata: dict[str, Any]  # Event metadata (user_id, tool_name, etc.)


@dataclass(frozen=True)
class SendOutcome:
    """
    Result of sending payload to Aiceberg API.

    Why: Encapsulates API response for easier handling of block decisions.
    """
    event_id: str
    event_result: str    # "passed", "blocked", "rejected"
    blocked: bool        # True if event was blocked
    reason: str          # Block reason if blocked
    raw: dict[str, Any]  # Full API response

    @classmethod
    def from_response(cls, response: dict[str, Any] | None) -> "SendOutcome":
        """Parse Aiceberg API response into SendOutcome."""
        resp = response or {}
        event_result = str(resp.get("event_result", "")).strip()
        blocked = event_result.lower() in {"block", "blocked", "rejected"}
        reason = _reason_from_response(resp, "")
        return cls(
            event_id=str(resp.get("event_id", "")).strip(),
            event_result=event_result,
            blocked=blocked,
            reason=reason,
            raw=resp,
        )


@dataclass(frozen=True)
class GenericHookSpec:
    """
    Specification for simple one-shot events (telemetry, notifications, etc.).

    Why: Standardizes handling of events that just need to be logged/sent
    without complex INPUT/OUTPUT pairing logic.
    """
    event_type: str      # Event type to send (usually "agt_agt")
    output_text: str     # OUTPUT content
    source: str          # Source identifier for metadata
    content_builder: Callable[[dict[str, Any]], Any]


def _now_epoch() -> int:
    return int(time.time())


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=True)


def _bool_env_or_default(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _int_env_or_default(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _log(msg: str) -> None:
    print(f"[aiceberg-hooks] {msg}", file=sys.stderr, flush=True)


def _resolve_script_dir() -> str:
    return os.path.dirname(os.path.realpath(__file__))


def _resolve_plugin_root() -> str:
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if env_root:
        return os.path.realpath(env_root)
    return os.path.realpath(os.path.join(_resolve_script_dir(), ".."))


def _parse_dotenv_file(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    continue

                # Remove inline comments only for unquoted values.
                if value and value[0] not in ("'", '"'):
                    hash_idx = value.find("#")
                    if hash_idx >= 0:
                        value = value[:hash_idx].strip()

                # Strip matching quotes.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]

                result[key] = value
    except Exception as exc:
        _log(f"warning: failed reading .env at {path}: {exc}")
    return result


def _load_dotenv_into_env() -> None:
    plugin_root = _resolve_plugin_root()
    candidates = [
        os.path.join(plugin_root, ".env"),
        os.path.join(plugin_root, "config", ".env"),
    ]
    for path in candidates:
        rp = os.path.realpath(path)
        if not os.path.isfile(rp):
            continue
        parsed = _parse_dotenv_file(rp)
        for key, value in parsed.items():
            if key not in os.environ:
                os.environ[key] = value
        _log(f"loaded env file: {rp}")
        break


def _normalize_placeholder(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith("<YOUR_") and stripped.endswith(">"):
        return ""
    return value


def _load_config_file() -> dict[str, Any]:
    plugin_root = _resolve_plugin_root()
    candidates = [
        os.path.join(plugin_root, "config", "config.json"),
        os.path.join(_resolve_script_dir(), "..", "config", "config.json"),
    ]
    for path in candidates:
        rp = os.path.realpath(path)
        if os.path.isfile(rp):
            try:
                with open(rp, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                _log(f"warning: failed to parse config at {rp}: {exc}")
    return {}


# ============================================================================
# CONFIGURATION LOADING
# ============================================================================
# Functions for loading configuration from .env, config.json, and environment
# variables. Configuration controls API endpoints, credentials, and behavior.
# ============================================================================

def load_config() -> dict[str, Any]:
    """
    Load configuration from all sources and return merged config dict.

    Priority order (highest to lowest):
      1. Environment variables (AICEBERG_*)
      2. config.json file
      3. Hard-coded defaults

    Why: Allows flexibility - can override via env vars without changing files.
    """
    _load_dotenv_into_env()

    cfg = {
        "base_url": "https://api.test1.aiceberg.ai",
        "event_url": "",
        "api_key": "",
        "profile_id": "",
        "use_case_id": "",
        "default_user_id": "cowork_agent",
        "enabled": True,
        "mode": "enforce",  # enforce | observe
        "fail_open": True,
        "timeout_seconds": 15,
        "redact_secrets": True,
        "forward_to_llm": False,
        "log_locally": True,
        "log_path": "",
        "db_path": "",
        "max_content_chars": MAX_CONTENT_CHARS,
        "mock_mode": False,
        "mock_block_tokens": "jailbreak,toxic,malware,rm -rf /,[[block]]",
        "dry_run_no_send": False,
        "print_payloads": False,
        "tiny_debug_mode": False,
        "debug_trace": False,
        "debug_trace_path": "",
        "skip_telemetry_api_send": True,  # Don't send telemetry events to API (log only)
        "llm_transcript_local_only": True,  # Don't send agt_llm transcript events to API (log only, for initial testing)
    }
    cfg.update(_load_config_file())

    # Clear legacy placeholder tokens if present in config files.
    for key in ("base_url", "event_url", "api_key", "profile_id", "use_case_id", "default_user_id"):
        cfg[key] = _normalize_placeholder(cfg.get(key))

    env_map = {
        "AICEBERG_BASE_URL": "base_url",
        "AICEBERG_API_URL": "event_url",
        "AICEBERG_EVENT_URL": "event_url",
        "AICEBERG_API_KEY": "api_key",
        "AICEBERG_PROFILE_ID": "profile_id",
        "AICEBERG_USE_CASE_ID": "use_case_id",
        "USE_CASE_ID": "use_case_id",
        "AICEBERG_USER_ID": "default_user_id",
        "AICEBERG_DEFAULT_USER_ID": "default_user_id",
        "AICEBERG_MODE": "mode",
        "AICEBERG_LOG_PATH": "log_path",
        "AICEBERG_DB_PATH": "db_path",
        "AICEBERG_DEBUG_TRACE_PATH": "debug_trace_path",
    }
    for env_name, cfg_key in env_map.items():
        env_val = os.environ.get(env_name)
        if env_val:
            cfg[cfg_key] = env_val

    cfg["enabled"] = _bool_env_or_default(os.environ.get("AICEBERG_ENABLED"), bool(cfg.get("enabled", True)))
    cfg["fail_open"] = _bool_env_or_default(os.environ.get("AICEBERG_FAIL_OPEN"), bool(cfg.get("fail_open", True)))
    cfg["redact_secrets"] = _bool_env_or_default(
        os.environ.get("AICEBERG_REDACT_SECRETS"), bool(cfg.get("redact_secrets", True))
    )
    cfg["log_locally"] = _bool_env_or_default(os.environ.get("AICEBERG_LOG_LOCALLY"), bool(cfg.get("log_locally", True)))
    cfg["forward_to_llm"] = _bool_env_or_default(
        os.environ.get("AICEBERG_FORWARD_TO_LLM"), bool(cfg.get("forward_to_llm", False))
    )
    cfg["timeout_seconds"] = _int_env_or_default(
        os.environ.get("AICEBERG_TIMEOUT"), int(cfg.get("timeout_seconds", 15))
    )
    cfg["max_content_chars"] = _int_env_or_default(
        os.environ.get("AICEBERG_MAX_CONTENT_CHARS"), int(cfg.get("max_content_chars", MAX_CONTENT_CHARS))
    )
    cfg["mock_mode"] = _bool_env_or_default(os.environ.get("AICEBERG_MOCK_MODE"), bool(cfg.get("mock_mode", False)))
    if os.environ.get("AICEBERG_MOCK_BLOCK_TOKENS"):
        cfg["mock_block_tokens"] = os.environ["AICEBERG_MOCK_BLOCK_TOKENS"]
    cfg["dry_run_no_send"] = _bool_env_or_default(
        os.environ.get("AICEBERG_DRY_RUN"), bool(cfg.get("dry_run_no_send", False))
    )
    cfg["print_payloads"] = _bool_env_or_default(
        os.environ.get("AICEBERG_PRINT_PAYLOADS"), bool(cfg.get("print_payloads", False))
    )
    cfg["tiny_debug_mode"] = _bool_env_or_default(
        os.environ.get("AICEBERG_TINY_DEBUG_MODE"), bool(cfg.get("tiny_debug_mode", False))
    )
    cfg["debug_trace"] = _bool_env_or_default(os.environ.get("AICEBERG_DEBUG_TRACE"), bool(cfg.get("debug_trace", False)))
    cfg["skip_telemetry_api_send"] = _bool_env_or_default(
        os.environ.get("AICEBERG_SKIP_TELEMETRY_API_SEND"), bool(cfg.get("skip_telemetry_api_send", True))
    )
    cfg["llm_transcript_local_only"] = _bool_env_or_default(
        os.environ.get("AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY"), bool(cfg.get("llm_transcript_local_only", True))
    )

    if not cfg.get("log_path"):
        cfg["log_path"] = os.path.join(_resolve_plugin_root(), "logs", "events.jsonl")
    if not cfg.get("db_path"):
        cfg["db_path"] = DEFAULT_DB_PATH
    if not cfg.get("debug_trace_path"):
        cfg["debug_trace_path"] = os.path.join(_resolve_plugin_root(), "logs", "debug-trace.jsonl")

    return cfg


def _event_endpoint(cfg: dict[str, Any]) -> str:
    event_url = str(cfg.get("event_url", "")).strip()
    if event_url:
        return event_url
    base_url = str(cfg.get("base_url", "")).strip().rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/eap/v1/event"):
        return base_url
    return f"{base_url}/eap/v1/event"


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 10:
        return value
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, val in value.items():
            low = key.lower()
            if any(token in low for token in REDACT_KEYS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = _redact(val, depth + 1)
        return redacted
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    return value


def _cap_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


# ============================================================================
# DATABASE - SQLite State Management
# ============================================================================
# Functions for managing persistent state across subprocess invocations.
# Why SQLite: Each hook runs as a separate process, needs shared state.
# Tables: open_events (INPUT events awaiting OUTPUT), links (tool/user IDs),
#         transcript_cursors (track processed LLM turns)
# ============================================================================

def _db_connect(db_path: str) -> sqlite3.Connection:
    """
    Connect to SQLite database and ensure schema is initialized.

    Why: Centralized connection point ensures consistent schema across all hooks.
    """
    rp = os.path.realpath(db_path)
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    conn = sqlite3.connect(rp, timeout=5)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS open_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            session_id TEXT NOT NULL,
            input_content TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            link_key TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcript_cursors (
            cursor_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            transcript_path TEXT NOT NULL,
            last_turn_index INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _cleanup_stale(conn: sqlite3.Connection, ttl_seconds: int) -> None:
    threshold = _now_epoch() - ttl_seconds
    stale_ids = [
        row[0]
        for row in conn.execute("SELECT event_id FROM open_events WHERE created_at < ?", (threshold,)).fetchall()
    ]
    if stale_ids:
        conn.executemany("DELETE FROM links WHERE event_id = ?", [(eid,) for eid in stale_ids])
        conn.executemany("DELETE FROM open_events WHERE event_id = ?", [(eid,) for eid in stale_ids])
    conn.execute("DELETE FROM transcript_cursors WHERE updated_at < ?", (threshold,))
    conn.commit()


def _store_open_event(
    conn: sqlite3.Connection,
    event_id: str,
    event_type: str,
    session_id: str,
    input_content: str,
    metadata: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO open_events VALUES (?,?,?,?,?,?)",
        (event_id, event_type, session_id, input_content, _safe_json_dumps(metadata), _now_epoch()),
    )
    conn.commit()


def _get_open_event(conn: sqlite3.Connection, event_id: str) -> OpenEventRecord | None:
    row = conn.execute(
        "SELECT event_id, event_type, session_id, input_content, metadata_json FROM open_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if not row:
        return None
    metadata: dict[str, Any]
    try:
        metadata = json.loads(row[4]) if row[4] else {}
    except Exception:
        metadata = {}
    return OpenEventRecord(
        event_id=row[0],
        event_type=row[1],
        session_id=row[2],
        input_content=row[3],
        metadata=metadata,
    )


def _close_open_event(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("DELETE FROM open_events WHERE event_id=?", (event_id,))
    conn.execute("DELETE FROM links WHERE event_id=?", (event_id,))
    conn.commit()


def _store_link(conn: sqlite3.Connection, link_key: str, event_id: str, session_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO links VALUES (?,?,?,?)",
        (link_key, event_id, session_id, _now_epoch()),
    )
    conn.commit()


def _pop_link(conn: sqlite3.Connection, link_key: str) -> str | None:
    row = conn.execute("SELECT event_id FROM links WHERE link_key=?", (link_key,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM links WHERE link_key=?", (link_key,))
    conn.commit()
    return row[0]


def _get_link(conn: sqlite3.Connection, link_key: str) -> str | None:
    row = conn.execute("SELECT event_id FROM links WHERE link_key=?", (link_key,)).fetchone()
    return row[0] if row else None


def _drain_session_open_events(conn: sqlite3.Connection, session_id: str) -> list[OpenEventRecord]:
    rows = conn.execute(
        "SELECT event_id, event_type, input_content, metadata_json FROM open_events WHERE session_id=?",
        (session_id,),
    ).fetchall()
    result: list[OpenEventRecord] = []
    for row in rows:
        metadata: dict[str, Any]
        try:
            metadata = json.loads(row[3]) if row[3] else {}
        except Exception:
            metadata = {}
        result.append(
            OpenEventRecord(
                event_id=row[0],
                event_type=row[1],
                session_id=session_id,
                input_content=row[2],
                metadata=metadata,
            )
        )
    conn.execute("DELETE FROM open_events WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM links WHERE session_id=?", (session_id,))
    conn.commit()
    return result


def _transcript_cursor_key(session_id: str, transcript_path: str) -> str:
    return f"{session_id}::{os.path.realpath(transcript_path)}"


def _get_transcript_cursor(conn: sqlite3.Connection, cursor_key: str) -> int:
    row = conn.execute("SELECT last_turn_index FROM transcript_cursors WHERE cursor_key=?", (cursor_key,)).fetchone()
    return int(row[0]) if row else -1


def _set_transcript_cursor(
    conn: sqlite3.Connection, cursor_key: str, session_id: str, transcript_path: str, last_turn_index: int
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO transcript_cursors VALUES (?,?,?,?,?)",
        (cursor_key, session_id, os.path.realpath(transcript_path), int(last_turn_index), _now_epoch()),
    )
    conn.commit()


def _clear_transcript_cursors_for_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM transcript_cursors WHERE session_id=?", (session_id,))
    conn.commit()


# ============================================================================
# LOCAL LOGGING
# ============================================================================
# Functions for writing events to local JSONL log file.
# Why: Provides full audit trail even when events aren't sent to API.
# All events (security + telemetry) are always logged locally.
# ============================================================================

def _append_local_log(cfg: dict[str, Any], payload: dict[str, Any], response: dict[str, Any]) -> None:
    """
    Append event to local JSONL log file.

    Why: Even when not sending to API, we want local record for debugging/audit.
    Format: One JSON object per line with timestamp, payload, response.
    """
    if not cfg.get("log_locally", True):
        return
    log_path = os.path.realpath(str(cfg.get("log_path", DEFAULT_LOG_PATH)))
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "response": response,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_safe_json_dumps(entry) + "\n")
    except Exception as exc:
        _log(f"warning: local log write failed: {exc}")


def _print_payload(payload: dict[str, Any], *, mode: str) -> None:
    compact = _safe_json_dumps(payload)
    _log(f"{mode} payload: {compact}")


def _append_debug_trace(cfg: dict[str, Any], trace: dict[str, Any]) -> None:
    if not cfg.get("debug_trace", False):
        return
    path = os.path.realpath(str(cfg.get("debug_trace_path", "")))
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **trace}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(_safe_json_dumps(entry) + "\n")
    except Exception as exc:
        _log(f"warning: debug trace write failed: {exc}")


def _post_aiceberg(payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    url = _event_endpoint(cfg)
    if not url:
        return {"event_result": "passed", "reason": "No endpoint configured (log-only mode)"}

    body = _safe_json_dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"aiceberg-claude-hooks/{VERSION}",
    }
    if cfg.get("api_key"):
        headers["Authorization"] = str(cfg["api_key"])

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    ctx = None
    if os.environ.get("AICEBERG_INSECURE", "0") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    timeout = int(cfg.get("timeout_seconds", 15))
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        resp_body = resp.read().decode("utf-8")
        return json.loads(resp_body) if resp_body.strip() else {}


# ============================================================================
# AICEBERG API - Network Communication
# ============================================================================
# Functions for sending payloads to Aiceberg API and handling responses.
# Includes mock mode, dry-run mode, and error handling.
# Why separate from payload building: Clean separation of concerns.
# ============================================================================

def _send_payload(payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Send payload to Aiceberg API (or mock/dry-run) and return response.

    Modes:
      - Normal: Send to API, return real response
      - Dry-run: Print payload, don't send, return mock "passed"
      - Mock: Local keyword matching, return "blocked" if keywords found

    Why: Central point for all API communication, makes testing easier.
    """
    if cfg.get("print_payloads", False) or cfg.get("dry_run_no_send", False):
        _print_payload(payload, mode="dry-run" if cfg.get("dry_run_no_send", False) else "send")

    if not cfg.get("enabled", True):
        response = {"event_result": "passed", "disabled": True}
        _append_local_log(cfg, payload, response)
        return response

    if cfg.get("dry_run_no_send", False):
        is_update = bool(payload.get("event_id"))
        event_id = str(payload.get("event_id", "")).strip() if is_update else str(uuid.uuid4())
        response = {
            "event_id": event_id,
            "event_result": "passed",
            "reason": "dry_run_no_send",
            "dry_run": True,
        }
        _append_local_log(cfg, payload, response)
        return response

    if cfg.get("mock_mode", False):
        block_tokens_raw = str(cfg.get("mock_block_tokens", ""))
        tokens = [t.strip().lower() for t in block_tokens_raw.split(",") if t.strip()]
        is_update = bool(payload.get("event_id"))
        text = str(payload.get("output" if is_update else "input", ""))
        text_low = text.lower()
        hit = next((tok for tok in tokens if tok in text_low), None)
        if is_update:
            event_id = str(payload.get("event_id", ""))
        else:
            event_id = str(uuid.uuid4())
        if hit:
            response = {
                "event_id": event_id,
                "event_result": "blocked",
                "policy": "mock_policy",
                "reason": f"blocked by token '{hit}'",
            }
        else:
            response = {
                "event_id": event_id,
                "event_result": "passed",
                "reason": "mock pass",
            }
        _append_local_log(cfg, payload, response)
        return response

    try:
        response = _post_aiceberg(payload, cfg)
        _append_local_log(cfg, payload, response)
        return response
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        error_msg = f"HTTP {exc.code}: {body}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        error_msg = f"Transport error: {exc}"
    except Exception as exc:
        error_msg = f"Unexpected send error: {exc}"

    response = {"event_result": "passed", "error": error_msg, "fail_open": True}
    if not cfg.get("fail_open", True):
        response = {"event_result": "block", "error": error_msg, "reason": error_msg}
    _append_local_log(cfg, payload, response)
    return response


def _is_blocked(response: dict[str, Any] | None) -> bool:
    """
    Check if Aiceberg API response indicates content was blocked.

    Why: Centralized check for all block statuses (block, blocked, rejected).
    Returns False for None/empty responses (fail-open behavior).
    """
    if not response:
        return False
    result = str(response.get("event_result", "")).strip().lower()
    return result in {"block", "blocked", "rejected"}


def _reason_from_response(response: dict[str, Any] | None, fallback: str) -> str:
    """
    Extract block reason from Aiceberg API response.

    Why: Different response formats may have reason in different fields.
    Fallback ensures we always have a message to return to user.
    """
    if not response:
        return fallback
    policy = str(response.get("policy", "")).strip()
    reason = str(response.get("reason", "")).strip()
    if policy and reason:
        return f"Policy: {policy} - {reason}"
    if policy:
        return f"Policy: {policy}"
    if reason:
        return reason
    return fallback


def _normalize_text_payload(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        return _cap_text(value, max_chars)
    return _cap_text(_safe_json_dumps(value), max_chars)


def _pick_fields(data: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: data.get(key) for key in keys if key in data}


def _generic_content_setup(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "Setup",
        "session_id": data.get("session_id", ""),
        "cwd": data.get("cwd", ""),
        "argv": data.get("argv", []),
    }


def _generic_content_session_start(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "SessionStart",
        "session_id": data.get("session_id", ""),
        "source": data.get("source", ""),
        "resume": bool(data.get("resume", False)),
    }


def _generic_content_notification(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "Notification",
        "session_id": data.get("session_id", ""),
        "message": data.get("message", ""),
        "level": data.get("level", ""),
    }


def _generic_content_subagent_start(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "SubagentStart",
        "session_id": data.get("session_id", ""),
        "agent_id": data.get("agent_id", ""),
        "agent_type": data.get("agent_type", ""),
    }


def _generic_content_teammate_idle(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "TeammateIdle",
        "session_id": data.get("session_id", ""),
        "teammate_id": data.get("teammate_id", ""),
        "idle_seconds": data.get("idle_seconds", 0),
    }


def _generic_content_task_completed(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "TaskCompleted",
        "session_id": data.get("session_id", ""),
        "task_id": data.get("task_id", ""),
        "status": data.get("status", ""),
        "summary": data.get("summary", ""),
    }


def _generic_content_config_change(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "ConfigChange",
        "session_id": data.get("session_id", ""),
        "changed_keys": data.get("changed_keys", []),
        "change_source": data.get("source", ""),
    }


def _generic_content_worktree_create(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "WorktreeCreate",
        "session_id": data.get("session_id", ""),
        "worktree_path": data.get("worktree_path", ""),
        "branch": data.get("branch", ""),
    }


def _generic_content_worktree_remove(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "WorktreeRemove",
        "session_id": data.get("session_id", ""),
        "worktree_path": data.get("worktree_path", ""),
    }


def _generic_content_precompact(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "PreCompact",
        "session_id": data.get("session_id", ""),
        "transcript_path": data.get("transcript_path", ""),
        "estimated_tokens": data.get("estimated_tokens", ""),
    }


GENERIC_HOOK_SPECS: dict[str, GenericHookSpec] = {
    "Setup": GenericHookSpec(
        event_type="agt_agt",
        output_text="[setup_ack]",
        source="setup",
        content_builder=_generic_content_setup,
    ),
    "SessionStart": GenericHookSpec(
        event_type="agt_agt",
        output_text="[session_started]",
        source="session_start",
        content_builder=_generic_content_session_start,
    ),
    "Notification": GenericHookSpec(
        event_type="agt_agt",
        output_text="[notification_ack]",
        source="notification",
        content_builder=_generic_content_notification,
    ),
    "SubagentStart": GenericHookSpec(
        event_type="agt_agt",
        output_text="[subagent_started]",
        source="subagent_start",
        content_builder=_generic_content_subagent_start,
    ),
    "TeammateIdle": GenericHookSpec(
        event_type="agt_agt",
        output_text="[teammate_idle_seen]",
        source="teammate_idle",
        content_builder=_generic_content_teammate_idle,
    ),
    "TaskCompleted": GenericHookSpec(
        event_type="agt_agt",
        output_text="[task_completed_seen]",
        source="task_completed",
        content_builder=_generic_content_task_completed,
    ),
    "ConfigChange": GenericHookSpec(
        event_type="agt_agt",
        output_text="[config_change_seen]",
        source="config_change",
        content_builder=_generic_content_config_change,
    ),
    "WorktreeCreate": GenericHookSpec(
        event_type="agt_agt",
        output_text="[worktree_created]",
        source="worktree_create",
        content_builder=_generic_content_worktree_create,
    ),
    "WorktreeRemove": GenericHookSpec(
        event_type="agt_agt",
        output_text="[worktree_removed]",
        source="worktree_remove",
        content_builder=_generic_content_worktree_remove,
    ),
    "PreCompact": GenericHookSpec(
        event_type="agt_agt",
        output_text="[precompact_seen]",
        source="precompact",
        content_builder=_generic_content_precompact,
    ),
}


def _classify_tool_event_type(tool_name: str) -> str | None:
    low = (tool_name or "").lower()
    if tool_name == "Task":
        return "agt_agt"
    if "aiceberg" in low:
        return None
    if low.startswith("mcp__") and any(token in low for token in MEM_PATTERNS):
        return "agt_mem"
    return "agt_tool"


def _default_metadata(hook_name: str, data: dict[str, Any], user_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "user_id": user_id,
        "hook_event_name": hook_name,
    }
    session_id = data.get("session_id")
    if session_id:
        metadata["caller_session_id"] = session_id
    return metadata


# ============================================================================
# AICEBERG API - Payload Building
# ============================================================================
# Functions for constructing CREATE and UPDATE payloads for Aiceberg API.
# CREATE: Sends INPUT content, gets event_id
# UPDATE: Sends OUTPUT content, linked to event_id
# Why: Aiceberg requires paired events for conversation flow tracking.
# ============================================================================

def _build_create_payload(
    event_type: str,
    input_content: str,
    session_id: str,
    metadata: dict[str, Any],
    cfg: dict[str, Any],
    *,
    session_start: bool = False,
) -> dict[str, Any]:
    payload = {
        "input": input_content,
        "event_type": event_type,
        "profile_id": cfg.get("profile_id", ""),
        "session_id": session_id or "",
        "use_case_id": cfg.get("use_case_id", ""),
        "forward_to_llm": bool(cfg.get("forward_to_llm", False)),
        "metadata": metadata,
    }
    if session_start and event_type == "user_agt":
        payload["session_start"] = True
    return payload


def _build_update_payload(
    event_id: str,
    event_type: str,
    input_content: str,
    output_content: str,
    session_id: str,
    metadata: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "input": input_content,
        "output": output_content,
        "profile_id": cfg.get("profile_id", ""),
        "session_id": session_id or "",
        "use_case_id": cfg.get("use_case_id", ""),
        "forward_to_llm": bool(cfg.get("forward_to_llm", False)),
        "metadata": metadata,
    }


def _emit_block_decision(hook_name: str, reason: str) -> dict[str, Any]:
    if hook_name == "PreToolUse":
        return {
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
    if hook_name == "PermissionRequest":
        return {
            "decision": "block",
            "reason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }
    return {"decision": "block", "reason": reason}


def _load_transcript_entries(transcript_path: str) -> list[dict[str, Any]]:
    if not transcript_path or not os.path.isfile(transcript_path):
        return []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []

    entries: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                entries.append(parsed)
        except json.JSONDecodeError:
            continue
    return entries


def _flatten_transcript_block(block: list[dict[str, Any]]) -> str:
    """
    Flatten transcript block to text.
    Note: Role prefixes ([user], [assistant]) are NOT added because Aiceberg dashboard
    already shows symbols for user vs agent. Adding them clutters the display.
    """
    parts: list[str] = []
    for item in block:
        msg = item.get("message", {}) if isinstance(item, dict) else {}
        content = msg.get("content", "")
        if isinstance(content, str):
            if content:
                parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if not isinstance(piece, dict):
                    continue
                typ = piece.get("type")
                if typ == "text" and piece.get("text"):
                    txt = str(piece["text"])
                    parts.append(txt)
                elif typ == "tool_use":
                    parts.append(_safe_json_dumps({"tool_use": piece.get("name"), "input": piece.get("input", {})}))
                elif typ == "tool_result":
                    parts.append(_safe_json_dumps({"tool_result": piece.get("content", "")})[:5000])
    return "\n".join(parts)


def _extract_llm_turns(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    if not entries:
        return []
    turns: list[tuple[str, str]] = []
    last_assistant_end = -1
    i = 0
    while i < len(entries):
        if entries[i].get("type") != "assistant":
            i += 1
            continue
        start = i
        while i < len(entries) and entries[i].get("type") == "assistant":
            i += 1
        end = i

        input_start = last_assistant_end + 1
        input_block = entries[input_start:start]
        output_block = entries[start:end]
        turns.append((_flatten_transcript_block(input_block), _flatten_transcript_block(output_block)))
        last_assistant_end = end - 1
    return turns


def _extract_last_llm_turn(transcript_path: str) -> tuple[str, str]:
    turns = _extract_llm_turns(_load_transcript_entries(transcript_path))
    if not turns:
        return "", ""
    return turns[-1]


def _emit_transcript_llm_turns(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    hook_name: str,
    data: dict[str, Any],
    session_id: str,
    user_id: str,
    *,
    enforce: bool,
) -> dict[str, Any] | None:
    transcript_path = str(data.get("transcript_path", "")).strip()
    if not transcript_path:
        return None

    entries = _load_transcript_entries(transcript_path)
    turns = _extract_llm_turns(entries)
    if not turns:
        return None

    cursor_key = _transcript_cursor_key(session_id, transcript_path)
    last_turn_index = _get_transcript_cursor(conn, cursor_key)
    if last_turn_index >= len(turns):
        last_turn_index = -1
    start_turn = max(0, last_turn_index + 1)

    _append_debug_trace(
        cfg,
        {
            "phase": "llm_turn_scan",
            "hook_event_name": hook_name,
            "session_id": session_id,
            "transcript_path": transcript_path,
            "turns_total": len(turns),
            "turns_emitting_from": start_turn,
        },
    )

    # Check if LLM transcript events should be local-only (not sent to API)
    local_only = cfg.get("llm_transcript_local_only", True)

    for idx in range(start_turn, len(turns)):
        llm_input, llm_output = turns[idx]
        meta = _default_metadata(hook_name, data, user_id)
        meta["source"] = "transcript_turn"
        meta["transcript_path"] = transcript_path
        meta["llm_turn_index"] = idx

        content = _cap_text(llm_input, int(cfg["max_content_chars"]))
        output_content = _cap_text(llm_output, int(cfg["max_content_chars"]))

        if local_only:
            # Log locally only, don't send to API
            create_payload = _build_create_payload("agt_llm", content, session_id, meta, cfg)
            update_payload = _build_update_payload(
                f"local-llm-{idx}",
                "agt_llm",
                content,
                output_content,
                session_id,
                meta,
                cfg,
            )
            _append_local_log(cfg, create_payload, {"event_result": "llm_local_only", "reason": "transcript reconstruction (local-only mode)"})
            _append_local_log(cfg, update_payload, {"event_result": "llm_local_only", "reason": "transcript reconstruction (local-only mode)"})
            _set_transcript_cursor(conn, cursor_key, session_id, transcript_path, idx)
            continue

        # Normal flow: send to API
        create_resp = _send_payload(_build_create_payload("agt_llm", content, session_id, meta, cfg), cfg)
        llm_event_id = str(create_resp.get("event_id", "")).strip()
        if not llm_event_id:
            continue

        _store_open_event(conn, llm_event_id, "agt_llm", session_id, content, meta)
        update_resp = _send_payload(
            _build_update_payload(
                llm_event_id,
                "agt_llm",
                content,
                output_content,
                session_id,
                meta,
                cfg,
            ),
            cfg,
        )
        _close_open_event(conn, llm_event_id)
        _set_transcript_cursor(conn, cursor_key, session_id, transcript_path, idx)

        if enforce and _is_blocked(update_resp) and hook_name in BLOCK_CAPABLE_HOOKS:
            reason = _reason_from_response(update_resp, "LLM output blocked by Aiceberg policy.")
            _close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return _emit_block_decision(hook_name, reason)

    if start_turn >= len(turns):
        _set_transcript_cursor(conn, cursor_key, session_id, transcript_path, len(turns) - 1)
    return None


def _close_session_open_events_with_reason(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    session_id: str,
    reason: str,
) -> None:
    """
    Close all open events for a session with a policy block message.
    Note: We send just the reason text - Aiceberg dashboard shows the block status visually.
    """
    policy_text = _cap_text(reason, int(cfg["max_content_chars"]))
    for evt in _drain_session_open_events(conn, session_id):
        _send_payload(
            _build_update_payload(
                evt.event_id,
                evt.event_type,
                evt.input_content,
                policy_text,
                evt.session_id,
                evt.metadata,
                cfg,
            ),
            cfg,
        )


def _one_shot_event(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    hook_name: str,
    data: dict[str, Any],
    *,
    event_type: str = "agt_agt",
    content_obj: Any | None = None,
    output_text: str = "[ack]",
    metadata_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_id = str(data.get("session_id", ""))
    user_id = str(cfg.get("default_user_id", "cowork_agent"))
    metadata = _default_metadata(hook_name, data, user_id)
    if metadata_extra:
        metadata.update(metadata_extra)
    content = _normalize_text_payload(content_obj if content_obj is not None else data, int(cfg["max_content_chars"]))

    # Check if this is a telemetry-only hook and skip API send if configured
    skip_api = cfg.get("skip_telemetry_api_send", True) and hook_name in TELEMETRY_ONLY_HOOKS

    if skip_api:
        # Log locally but don't send to API
        create_payload = _build_create_payload(event_type, content, session_id, metadata, cfg, session_start=False)
        update_payload = _build_update_payload("local-" + hook_name, event_type, content, output_text, session_id, metadata, cfg)
        _append_local_log(cfg, create_payload, {"event_result": "telemetry_skipped", "reason": "telemetry-only hook"})
        _append_local_log(cfg, update_payload, {"event_result": "telemetry_skipped", "reason": "telemetry-only hook"})
        return {"event_result": "passed", "event_id": None, "telemetry_only": True}

    create_payload = _build_create_payload(event_type, content, session_id, metadata, cfg, session_start=False)
    create_resp = _send_payload(create_payload, cfg)
    event_id = str(create_resp.get("event_id", "")).strip()
    if not event_id:
        return create_resp

    _store_open_event(conn, event_id, event_type, session_id, content, metadata)
    update_payload = _build_update_payload(event_id, event_type, content, output_text, session_id, metadata, cfg)
    update_resp = _send_payload(update_payload, cfg)
    _close_open_event(conn, event_id)
    if _is_blocked(update_resp):
        return update_resp
    return create_resp


def _handle_generic_hook_with_spec(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    hook_name: str,
    data: dict[str, Any],
    *,
    enforce: bool,
    session_id: str,
) -> dict[str, Any] | None:
    spec = GENERIC_HOOK_SPECS.get(hook_name)
    if not spec:
        return None
    content_obj = spec.content_builder(data)
    resp = _one_shot_event(
        conn,
        cfg,
        hook_name,
        data,
        event_type=spec.event_type,
        content_obj=content_obj,
        output_text=spec.output_text,
        metadata_extra={"source": spec.source},
    )
    outcome = SendOutcome.from_response(resp)
    if enforce and hook_name in BLOCK_CAPABLE_HOOKS and outcome.blocked:
        reason = outcome.reason or f"{hook_name} blocked by Aiceberg policy."
        _close_session_open_events_with_reason(conn, cfg, session_id, reason)
        return _emit_block_decision(hook_name, reason)
    return {}


# ============================================================================
# MAIN ENTRY POINT - Hook Event Dispatcher
# ============================================================================

def handle_hook_event(conn: sqlite3.Connection, cfg: dict[str, Any], hook_name: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Main dispatcher for all Claude hook events.

    This is the entry point called by main(). Routes each hook to its
    appropriate handler based on hook_name.

    Flow:
      1. Check if enabled/debug mode
      2. Route to handler based on hook_name:
         - UserPromptSubmit → user_agt INPUT
         - PreToolUse → agt_tool/agt_mem/agt_agt INPUT
         - PostToolUse → OUTPUT
         - Stop → Parse transcript, emit agt_llm, close user_agt
         - SessionEnd → Cleanup
         - Others → Generic/telemetry handling
      3. If enforcing and blocked → close all events, return block decision
      4. Return decision dict (or empty dict to allow)

    Why: Centralized routing makes it easy to see all supported hooks and their logic.

    Args:
        conn: SQLite connection for state management
        cfg: Configuration dict
        hook_name: Name of hook event (e.g., "UserPromptSubmit")
        data: Hook payload data from Claude

    Returns:
        Decision dict for Claude runtime:
          - {} (empty) → Allow
          - {"decision": "block"} → Block execution
          - {"decision": "block", "hookSpecificOutput": {...}} → Block with details
    """
    enforce = str(cfg.get("mode", "enforce")).lower() == "enforce"
    envelope = HookEventEnvelope(hook_name=hook_name, session_id=str(data.get("session_id", "")), payload=data)
    session_id = envelope.session_id
    user_id = str(cfg.get("default_user_id", "cowork_agent"))

    if cfg.get("tiny_debug_mode", False) and hook_name not in TINY_DEBUG_HOOKS:
        _append_debug_trace(
            cfg,
            {
                "phase": "skip",
                "reason": "tiny_debug_mode",
                "hook_event_name": hook_name,
                "session_id": session_id,
            },
        )
        return {}

    if hook_name == "UserPromptSubmit":
        prompt = str(data.get("prompt", data.get("user_prompt", "")))
        metadata = _default_metadata(hook_name, data, user_id)
        metadata["source"] = "user_prompt_submit"
        create_payload = _build_create_payload("user_agt", _cap_text(prompt, int(cfg["max_content_chars"])), session_id, metadata, cfg)
        resp = _send_payload(create_payload, cfg)
        event_id = str(resp.get("event_id", "")).strip()
        if event_id:
            _store_open_event(conn, event_id, "user_agt", session_id, prompt, metadata)
            _store_link(conn, f"user:{session_id}", event_id, session_id)
        if enforce and _is_blocked(resp):
            reason = _reason_from_response(resp, "User prompt blocked by Aiceberg policy.")
            _close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return _emit_block_decision(hook_name, reason)
        return {}

    if hook_name == "PreToolUse":
        tool_name = str(data.get("tool_name", ""))
        event_type = _classify_tool_event_type(tool_name)
        if not event_type:
            return {}
        tool_use_id = str(data.get("tool_use_id", ""))
        content_obj = {"tool_name": tool_name, "tool_input": data.get("tool_input", {}), "tool_use_id": tool_use_id}
        content = _normalize_text_payload(content_obj, int(cfg["max_content_chars"]))
        metadata = _default_metadata(hook_name, data, user_id)
        metadata.update({"tool_name": tool_name, "tool_use_id": tool_use_id})
        resp = _send_payload(_build_create_payload(event_type, content, session_id, metadata, cfg), cfg)
        event_id = str(resp.get("event_id", "")).strip()
        if event_id:
            _store_open_event(conn, event_id, event_type, session_id, content, metadata)
            if tool_use_id:
                _store_link(conn, f"tool:{tool_use_id}", event_id, session_id)
        if enforce and _is_blocked(resp):
            reason = _reason_from_response(resp, "Tool call blocked by Aiceberg policy.")
            if event_id:
                _send_payload(_build_update_payload(event_id, event_type, content, reason, session_id, metadata, cfg), cfg)
                _close_open_event(conn, event_id)
            _close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return _emit_block_decision(hook_name, reason)
        return {}

    if hook_name in {"PostToolUse", "PostToolUseFailure"}:
        tool_use_id = str(data.get("tool_use_id", ""))
        if not tool_use_id:
            return {}
        event_id = _pop_link(conn, f"tool:{tool_use_id}")
        if not event_id:
            return {}
        open_evt = _get_open_event(conn, event_id)
        if not open_evt:
            return {}
        if hook_name == "PostToolUseFailure":
            output = _normalize_text_payload({"error": data.get("error", "unknown error"), "is_interrupt": data.get("is_interrupt", False)}, int(cfg["max_content_chars"]))
        else:
            output = _normalize_text_payload(data.get("tool_response", ""), int(cfg["max_content_chars"]))
        resp = _send_payload(
            _build_update_payload(
                event_id,
                open_evt.event_type,
                open_evt.input_content,
                output,
                open_evt.session_id,
                open_evt.metadata,
                cfg,
            ),
            cfg,
        )
        _close_open_event(conn, event_id)
        if enforce and hook_name == "PostToolUse" and _is_blocked(resp):
            reason = _reason_from_response(resp, "Tool result blocked by Aiceberg policy.")
            _close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return _emit_block_decision(hook_name, reason)
        return {}

    if hook_name == "PermissionRequest":
        tool_name = str(data.get("tool_name", ""))
        event_type = _classify_tool_event_type(tool_name) or "agt_tool"
        resp = _one_shot_event(
            conn,
            cfg,
            hook_name,
            data,
            event_type=event_type,
            content_obj={
                "tool_name": tool_name,
                "tool_input": data.get("tool_input", {}),
                "permission_suggestions": data.get("permission_suggestions", []),
            },
            output_text="[permission_reviewed]",
            metadata_extra={"source": "permission_request"},
        )
        if enforce and _is_blocked(resp):
            reason = _reason_from_response(resp, "Permission request blocked by Aiceberg policy.")
            _close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return _emit_block_decision(hook_name, reason)
        return {}

    if hook_name == "Stop":
        if bool(data.get("stop_hook_active", False)):
            return {}

        # Close user_agt open event for this session.
        user_event_id = _get_link(conn, f"user:{session_id}")
        transcript_path = str(data.get("transcript_path", ""))
        _, llm_output = _extract_last_llm_turn(transcript_path)

        if user_event_id:
            open_evt = _get_open_event(conn, user_event_id)
            if open_evt:
                output = _cap_text(llm_output or "No response", int(cfg["max_content_chars"]))
                resp = _send_payload(
                    _build_update_payload(
                        user_event_id,
                        open_evt.event_type,
                        open_evt.input_content,
                        output,
                        open_evt.session_id,
                        open_evt.metadata,
                        cfg,
                    ),
                    cfg,
                )
                _close_open_event(conn, user_event_id)
                if enforce and _is_blocked(resp):
                    reason = _reason_from_response(resp, "Final response blocked by Aiceberg policy.")
                    _close_session_open_events_with_reason(conn, cfg, session_id, reason)
                    return _emit_block_decision(hook_name, reason)

        llm_decision = _emit_transcript_llm_turns(conn, cfg, hook_name, data, session_id, user_id, enforce=enforce)
        if llm_decision:
            return llm_decision
        return {}

    if hook_name == "SubagentStop":
        if bool(data.get("stop_hook_active", False)):
            return {}
        transcript_path = str(data.get("transcript_path", ""))
        llm_input, llm_output = _extract_last_llm_turn(transcript_path)
        llm_decision = _emit_transcript_llm_turns(conn, cfg, hook_name, data, session_id, user_id, enforce=enforce)
        if llm_decision:
            return llm_decision
        if llm_input or llm_output:
            resp = _one_shot_event(
                conn,
                cfg,
                hook_name,
                data,
                event_type="agt_agt",
                content_obj={
                    "agent_id": data.get("agent_id", ""),
                    "agent_transcript_path": data.get("agent_transcript_path", ""),
                    "llm_input": llm_input,
                    "llm_output": llm_output,
                },
                output_text="[subagent_stop_captured]",
            )
            if enforce and _is_blocked(resp):
                reason = _reason_from_response(resp, "Subagent result blocked by Aiceberg policy.")
                _close_session_open_events_with_reason(conn, cfg, session_id, reason)
                return _emit_block_decision(hook_name, reason)
        return {}

    if hook_name == "SessionEnd":
        for evt in _drain_session_open_events(conn, session_id):
            _send_payload(
                _build_update_payload(
                    evt.event_id,
                    evt.event_type,
                    evt.input_content,
                    "[session_end]",
                    evt.session_id,
                    evt.metadata,
                    cfg,
                ),
                cfg,
            )
        _clear_transcript_cursors_for_session(conn, session_id)
        _one_shot_event(conn, cfg, hook_name, data, event_type="agt_agt", output_text="[session_closed]")
        return {}

    spec_resp = _handle_generic_hook_with_spec(conn, cfg, hook_name, data, enforce=enforce, session_id=session_id)
    if spec_resp is not None:
        return spec_resp

    # Fallback generic lifecycle telemetry for any hook not explicitly modeled.
    generic_resp = _one_shot_event(
        conn,
        cfg,
        hook_name,
        data,
        event_type="agt_agt",
        metadata_extra={"source": "generic_hook"},
    )
    if enforce and hook_name in BLOCK_CAPABLE_HOOKS and _is_blocked(generic_resp):
        reason = _reason_from_response(generic_resp, f"{hook_name} blocked by Aiceberg policy.")
        _close_session_open_events_with_reason(conn, cfg, session_id, reason)
        return _emit_block_decision(hook_name, reason)
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Aiceberg Claude hook monitor")
    parser.add_argument("--event", default="", help="Hook event name override")
    args = parser.parse_args()

    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception as exc:
        _log(f"bad stdin json: {exc}")
        return 0

    hook_name = args.event or str(data.get("hook_event_name", "")).strip()
    if not hook_name:
        _log("warning: no hook_event_name provided")
        return 0

    cfg = load_config()
    max_chars = int(cfg.get("max_content_chars", MAX_CONTENT_CHARS))
    if max_chars <= 0:
        cfg["max_content_chars"] = MAX_CONTENT_CHARS

    conn = _db_connect(str(cfg.get("db_path", DEFAULT_DB_PATH)))
    try:
        _append_debug_trace(
            cfg,
            {
                "phase": "start",
                "hook_event_name": hook_name,
                "session_id": str(data.get("session_id", "")),
                "tiny_debug_mode": bool(cfg.get("tiny_debug_mode", False)),
            },
        )
        _cleanup_stale(conn, OPEN_EVENT_TTL_SECONDS)
        if cfg.get("redact_secrets", True):
            # Keep a redacted envelope in local audit logs for observability.
            preview = {"hook_event_name": hook_name, "session_id": data.get("session_id", ""), "payload": _redact(data)}
            _append_local_log(cfg, {"preview": preview}, {"event_result": "preview"})

        decision = handle_hook_event(conn, cfg, hook_name, data)
        _append_debug_trace(
            cfg,
            {
                "phase": "end",
                "hook_event_name": hook_name,
                "session_id": str(data.get("session_id", "")),
                "decision": decision if decision else {},
            },
        )
        if decision:
            print(_safe_json_dumps(decision))
    except Exception as exc:
        _log(f"handler error ({hook_name}): {exc}")
        _append_local_log(cfg, {"hook_event_name": hook_name, "payload": _redact(data)}, {"error": str(exc)})
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
