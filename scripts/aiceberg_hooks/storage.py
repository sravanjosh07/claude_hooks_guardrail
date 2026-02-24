"""Storage, configuration, and utilities for Aiceberg Claude hooks."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

# ============================================================================
# CONSTANTS
# ============================================================================

VERSION = "1.0.0"
DEFAULT_DB_PATH = "/tmp/aiceberg-claude-hooks/monitor.db"
DEFAULT_LOG_PATH = "/tmp/aiceberg-claude-hooks/events.jsonl"
MAX_CONTENT_CHARS = 50000
OPEN_EVENT_TTL_SECONDS = 1800

REDACT_KEYS = {
    "api_key",
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
}

MEM_PATTERNS = ("memory", "store", "save", "remember", "retrieve")

# ============================================================================
# DATA STRUCTURES
# ============================================================================


@dataclass(frozen=True)
class HookEventEnvelope:
    """Wrapper for hook event with metadata."""

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

    event_id: str  # Aiceberg event ID from CREATE response
    event_type: str  # user_agt, agt_llm, agt_tool, etc.
    session_id: str  # Claude session ID
    input_content: str  # Original INPUT content
    metadata: dict[str, Any]  # Event metadata (user_id, tool_name, etc.)


@dataclass(frozen=True)
class SendOutcome:
    """Result of sending a payload to Aiceberg API."""

    event_id: str
    event_result: str
    blocked: bool
    reason: str
    raw: dict[str, Any]

    @classmethod
    def from_response(cls, response: dict[str, Any] | None) -> "SendOutcome":
        resp = response or {}
        event_result = str(resp.get("event_result", "")).strip()
        blocked = event_result.lower() in {"block", "blocked", "rejected"}
        reason = reason_from_response(resp, "")
        return cls(
            event_id=str(resp.get("event_id", "")).strip(),
            event_result=event_result,
            blocked=blocked,
            reason=reason,
            raw=resp,
        )


@dataclass(frozen=True)
class GenericHookSpec:
    """Specification for generic hook event handling."""

    event_type: str
    output_text: str
    source: str
    content_builder: Callable[[dict[str, Any]], Any]


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def now_epoch() -> int:
    return int(time.time())


def safe_json_dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=True)


def bool_env_or_default(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def int_env_or_default(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def log(msg: str) -> None:
    """Log message to stderr."""
    print(f"[aiceberg-hooks] {msg}", file=sys.stderr, flush=True)


def cap_text(text: str, max_chars: int) -> str:
    """Truncate text to max_chars if longer."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def redact(value: Any, depth: int = 0) -> Any:
    """
    Recursively redact sensitive values from data structures.

    Why: Prevents secrets from appearing in logs or debug output.
    """
    if depth > 10:
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            low = key.lower()
            if any(token in low for token in REDACT_KEYS):
                result[key] = "***REDACTED***"
            else:
                result[key] = redact(item, depth + 1)
        return result
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value]
    return value


def normalize_text_payload(value: Any, max_chars: int) -> str:
    """Convert any value to capped string (JSON if not already string)."""
    if isinstance(value, str):
        return cap_text(value, max_chars)
    return cap_text(safe_json_dumps(value), max_chars)


def is_blocked(response: dict[str, Any] | None) -> bool:
    """
    Check if Aiceberg response indicates a block decision.

    Why fail-open: If response is None or unclear, we assume 'passed' to avoid
    blocking legitimate user activity when the API is unreachable.
    """
    if not response:
        return False
    result = str(response.get("event_result", "")).strip().lower()
    return result in {"block", "blocked", "rejected"}


def reason_from_response(response: dict[str, Any] | None, fallback: str) -> str:
    """
    Extract human-readable block reason from Aiceberg response.

    Tries: policy + reason, policy alone, reason alone, then fallback.
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


# ============================================================================
# CONFIGURATION LOADING
# ============================================================================


def resolve_script_dir() -> str:
    return os.path.dirname(os.path.realpath(__file__))


def resolve_plugin_root() -> str:
    """
    Resolve plugin root directory.

    Why: Supports both direct execution and installed package scenarios.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if env_root:
        return os.path.realpath(env_root)
    # package sits under scripts/aiceberg_hooks
    return os.path.realpath(os.path.join(resolve_script_dir(), "..", ".."))


def parse_dotenv_file(path: str) -> dict[str, str]:
    """Parse .env file into key-value dict."""
    parsed: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
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
                    comment_idx = value.find("#")
                    if comment_idx >= 0:
                        value = value[:comment_idx].strip()

                # Strip surrounding quotes.
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]

                parsed[key] = value
    except Exception as exc:
        log(f"warning: failed reading .env at {path}: {exc}")
    return parsed


def load_dotenv_into_env() -> None:
    """Load .env file into environment variables."""
    plugin_root = resolve_plugin_root()
    candidates = [
        os.path.join(plugin_root, ".env"),
        os.path.join(plugin_root, "config", ".env"),
    ]
    for path in candidates:
        resolved = os.path.realpath(path)
        if not os.path.isfile(resolved):
            continue
        parsed = parse_dotenv_file(resolved)
        for key, value in parsed.items():
            if key not in os.environ:
                os.environ[key] = value
        log(f"loaded env file: {resolved}")
        break


def normalize_placeholder(value: Any) -> Any:
    """Convert placeholder strings like <YOUR_API_KEY> to empty string."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped.startswith("<YOUR_") and stripped.endswith(">"):
        return ""
    return value


def load_config_file() -> dict[str, Any]:
    """Load config.json from standard locations."""
    plugin_root = resolve_plugin_root()
    candidates = [
        os.path.join(plugin_root, "config", "config.json"),
        os.path.join(plugin_root, "scripts", "config", "config.json"),
    ]
    for path in candidates:
        resolved = os.path.realpath(path)
        if not os.path.isfile(resolved):
            continue
        try:
            with open(resolved, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            log(f"warning: failed to parse config at {resolved}: {exc}")
    return {}


def load_config() -> dict[str, Any]:
    """
    Load and merge all configuration sources.

    Precedence: Environment variables > config.json > defaults

    Why: Allows easy testing (env overrides) while supporting production config files.
    """
    load_dotenv_into_env()

    cfg: dict[str, Any] = {
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
        # Telemetry filtering: Don't send SessionEnd, Setup, etc. to API
        "skip_telemetry_api_send": True,
        # LLM transcript local-only: Don't send LLM turns to API (test locally first)
        "llm_transcript_local_only": True,
    }
    cfg.update(load_config_file())

    for key in ("base_url", "event_url", "api_key", "profile_id", "use_case_id", "default_user_id"):
        cfg[key] = normalize_placeholder(cfg.get(key))

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

    cfg["enabled"] = bool_env_or_default(os.environ.get("AICEBERG_ENABLED"), bool(cfg.get("enabled", True)))
    cfg["fail_open"] = bool_env_or_default(os.environ.get("AICEBERG_FAIL_OPEN"), bool(cfg.get("fail_open", True)))
    cfg["redact_secrets"] = bool_env_or_default(
        os.environ.get("AICEBERG_REDACT_SECRETS"), bool(cfg.get("redact_secrets", True))
    )
    cfg["log_locally"] = bool_env_or_default(os.environ.get("AICEBERG_LOG_LOCALLY"), bool(cfg.get("log_locally", True)))
    cfg["forward_to_llm"] = bool_env_or_default(
        os.environ.get("AICEBERG_FORWARD_TO_LLM"), bool(cfg.get("forward_to_llm", False))
    )
    cfg["timeout_seconds"] = int_env_or_default(
        os.environ.get("AICEBERG_TIMEOUT"), int(cfg.get("timeout_seconds", 15))
    )
    cfg["max_content_chars"] = int_env_or_default(
        os.environ.get("AICEBERG_MAX_CONTENT_CHARS"), int(cfg.get("max_content_chars", MAX_CONTENT_CHARS))
    )
    cfg["mock_mode"] = bool_env_or_default(os.environ.get("AICEBERG_MOCK_MODE"), bool(cfg.get("mock_mode", False)))
    if os.environ.get("AICEBERG_MOCK_BLOCK_TOKENS"):
        cfg["mock_block_tokens"] = os.environ["AICEBERG_MOCK_BLOCK_TOKENS"]
    cfg["dry_run_no_send"] = bool_env_or_default(
        os.environ.get("AICEBERG_DRY_RUN"), bool(cfg.get("dry_run_no_send", False))
    )
    cfg["print_payloads"] = bool_env_or_default(
        os.environ.get("AICEBERG_PRINT_PAYLOADS"), bool(cfg.get("print_payloads", False))
    )
    cfg["tiny_debug_mode"] = bool_env_or_default(
        os.environ.get("AICEBERG_TINY_DEBUG_MODE"), bool(cfg.get("tiny_debug_mode", False))
    )
    cfg["debug_trace"] = bool_env_or_default(os.environ.get("AICEBERG_DEBUG_TRACE"), bool(cfg.get("debug_trace", False)))
    cfg["skip_telemetry_api_send"] = bool_env_or_default(
        os.environ.get("AICEBERG_SKIP_TELEMETRY_API_SEND"), bool(cfg.get("skip_telemetry_api_send", True))
    )
    cfg["llm_transcript_local_only"] = bool_env_or_default(
        os.environ.get("AICEBERG_LLM_TRANSCRIPT_LOCAL_ONLY"), bool(cfg.get("llm_transcript_local_only", True))
    )

    if not cfg.get("log_path"):
        cfg["log_path"] = os.path.join(resolve_plugin_root(), "logs", "events.jsonl")
    if not cfg.get("db_path"):
        cfg["db_path"] = DEFAULT_DB_PATH
    if not cfg.get("debug_trace_path"):
        cfg["debug_trace_path"] = os.path.join(resolve_plugin_root(), "logs", "debug-trace.jsonl")

    return cfg


# ============================================================================
# SQLITE STATE MANAGEMENT
# ============================================================================


def db_connect(db_path: str) -> sqlite3.Connection:
    """
    Connect to SQLite and ensure required schema exists.

    Why: Each hook runs in separate subprocess, so we need durable state storage.
    Schema tracks:
      - open_events: INPUT events waiting for OUTPUT
      - links: Associations (e.g., tool_use_id → event_id)
      - transcript_cursors: Track which LLM turns we've already processed
    """
    resolved = os.path.realpath(db_path)
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    conn = sqlite3.connect(resolved, timeout=5)
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


def cleanup_stale(conn: sqlite3.Connection, ttl_seconds: int) -> None:
    """
    Remove stale open events and cursors.

    Why: Prevent database bloat from orphaned events (crashed sessions, etc.)
    """
    threshold = now_epoch() - ttl_seconds
    stale_ids = [
        row[0]
        for row in conn.execute("SELECT event_id FROM open_events WHERE created_at < ?", (threshold,)).fetchall()
    ]
    if stale_ids:
        conn.executemany("DELETE FROM links WHERE event_id = ?", [(event_id,) for event_id in stale_ids])
        conn.executemany("DELETE FROM open_events WHERE event_id = ?", [(event_id,) for event_id in stale_ids])
    conn.execute("DELETE FROM transcript_cursors WHERE updated_at < ?", (threshold,))
    conn.commit()


def store_open_event(
    conn: sqlite3.Connection,
    event_id: str,
    event_type: str,
    session_id: str,
    input_content: str,
    metadata: dict[str, Any],
) -> None:
    """Store an INPUT event that's waiting for its OUTPUT."""
    conn.execute(
        "INSERT OR REPLACE INTO open_events VALUES (?,?,?,?,?,?)",
        (event_id, event_type, session_id, input_content, safe_json_dumps(metadata), now_epoch()),
    )
    conn.commit()


def get_open_event(conn: sqlite3.Connection, event_id: str) -> OpenEventRecord | None:
    """Retrieve an open event by its event_id."""
    row = conn.execute(
        "SELECT event_id, event_type, session_id, input_content, metadata_json FROM open_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    if not row:
        return None
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


def close_open_event(conn: sqlite3.Connection, event_id: str) -> None:
    """
    Remove an open event and its links.

    Why: Called after sending OUTPUT to Aiceberg (event is now complete).
    """
    conn.execute("DELETE FROM open_events WHERE event_id=?", (event_id,))
    conn.execute("DELETE FROM links WHERE event_id=?", (event_id,))
    conn.commit()


def store_link(conn: sqlite3.Connection, link_key: str, event_id: str, session_id: str) -> None:
    """
    Store a link (association) between a key and event_id.

    Example: link_key="tool:abc123" → event_id="evt_xyz" allows us to find
    the event later when we get PostToolUse with tool_use_id="abc123".
    """
    conn.execute(
        "INSERT OR REPLACE INTO links VALUES (?,?,?,?)",
        (link_key, event_id, session_id, now_epoch()),
    )
    conn.commit()


def pop_link(conn: sqlite3.Connection, link_key: str) -> str | None:
    """
    Retrieve and delete a link in one operation.

    Why: Tool outputs need to find their INPUT event, then remove the link.
    """
    row = conn.execute("SELECT event_id FROM links WHERE link_key=?", (link_key,)).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM links WHERE link_key=?", (link_key,))
    conn.commit()
    return row[0]


def get_link(conn: sqlite3.Connection, link_key: str) -> str | None:
    """Retrieve a link without deleting it."""
    row = conn.execute("SELECT event_id FROM links WHERE link_key=?", (link_key,)).fetchone()
    return row[0] if row else None


def drain_session_open_events(conn: sqlite3.Connection, session_id: str) -> list[OpenEventRecord]:
    """
    Retrieve and delete all open events for a session.

    Why: Called during SessionEnd or when blocking to close all pending events.
    """
    rows = conn.execute(
        "SELECT event_id, event_type, input_content, metadata_json FROM open_events WHERE session_id=?",
        (session_id,),
    ).fetchall()
    events: list[OpenEventRecord] = []
    for row in rows:
        try:
            metadata = json.loads(row[3]) if row[3] else {}
        except Exception:
            metadata = {}
        events.append(
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
    return events


def transcript_cursor_key(session_id: str, transcript_path: str) -> str:
    """Generate a unique key for tracking transcript processing progress."""
    return f"{session_id}::{os.path.realpath(transcript_path)}"


def get_transcript_cursor(conn: sqlite3.Connection, cursor_key: str) -> int:
    """Get the last processed turn index for a transcript (-1 if none)."""
    row = conn.execute("SELECT last_turn_index FROM transcript_cursors WHERE cursor_key=?", (cursor_key,)).fetchone()
    return int(row[0]) if row else -1


def set_transcript_cursor(
    conn: sqlite3.Connection,
    cursor_key: str,
    session_id: str,
    transcript_path: str,
    last_turn_index: int,
) -> None:
    """
    Update the cursor position for a transcript.

    Why: Prevents re-processing the same LLM turns on subsequent Stop hooks.
    """
    conn.execute(
        "INSERT OR REPLACE INTO transcript_cursors VALUES (?,?,?,?,?)",
        (cursor_key, session_id, os.path.realpath(transcript_path), int(last_turn_index), now_epoch()),
    )
    conn.commit()


def clear_transcript_cursors_for_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Remove all transcript cursors for a session (cleanup on SessionEnd)."""
    conn.execute("DELETE FROM transcript_cursors WHERE session_id=?", (session_id,))
    conn.commit()


# ============================================================================
# TRANSCRIPT PARSING
# ============================================================================


def load_transcript_entries(transcript_path: str) -> list[dict[str, Any]]:
    """
    Load transcript JSONL file into list of entries.

    Why: Claude doesn't expose BeforeModelCallEvent/AfterModelCallEvent, so we
    reconstruct LLM turns by reading the transcript file after execution.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return []
    try:
        with open(transcript_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return []

    entries: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def flatten_transcript_block(block: list[dict[str, Any]]) -> str:
    """
    Flatten transcript message content into plain text for Aiceberg payloads.

    Note: Role prefixes ([user], [assistant]) are NOT added because Aiceberg
    dashboard already shows symbols for user vs agent. Adding them clutters display.
    """
    parts: list[str] = []
    for item in block:
        message = item.get("message", {}) if isinstance(item, dict) else {}
        content = message.get("content", "")
        if isinstance(content, str):
            if content:
                parts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for piece in content:
            if not isinstance(piece, dict):
                continue
            piece_type = piece.get("type")
            if piece_type == "text" and piece.get("text"):
                parts.append(str(piece["text"]))
            elif piece_type == "tool_use":
                parts.append(safe_json_dumps({"tool_use": piece.get("name"), "input": piece.get("input", {})}))
            elif piece_type == "tool_result":
                parts.append(safe_json_dumps({"tool_result": piece.get("content", "")})[:5000])
    return "\n".join(parts)


def extract_llm_turns(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """
    Extract LLM turns from transcript entries.

    Returns: List of (input, output) tuples representing each LLM thinking cycle.

    Why: Aiceberg needs agt_llm events to monitor what the LLM is actually doing,
    but Claude only gives us the transcript after execution (not live hooks).
    """
    if not entries:
        return []
    turns: list[tuple[str, str]] = []
    last_assistant_end = -1
    index = 0
    while index < len(entries):
        if entries[index].get("type") != "assistant":
            index += 1
            continue
        start = index
        while index < len(entries) and entries[index].get("type") == "assistant":
            index += 1
        end = index

        input_start = last_assistant_end + 1
        input_block = entries[input_start:start]
        output_block = entries[start:end]
        turns.append((flatten_transcript_block(input_block), flatten_transcript_block(output_block)))
        last_assistant_end = end - 1
    return turns


def extract_last_llm_turn(transcript_path: str) -> tuple[str, str]:
    """
    Extract the most recent LLM turn from a transcript.

    Why: Stop hook needs the final LLM output to close the user_agt event.
    """
    turns = extract_llm_turns(load_transcript_entries(transcript_path))
    if not turns:
        return "", ""
    return turns[-1]
