"""API communication and payload building for Aiceberg events."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from .storage import (
    DEFAULT_LOG_PATH,
    MEM_PATTERNS,
    VERSION,
    GenericHookSpec,
    cap_text,
    log,
    normalize_text_payload,
    safe_json_dumps,
)

# ============================================================================
# PAYLOAD BUILDING
# ============================================================================


def classify_tool_event_type(tool_name: str) -> str | None:
    """
    Classify tool into event type (agt_tool, agt_mem, agt_agt, or None).

    Why:
      - agt_tool: Standard tools (Bash, Read, Write, etc.)
      - agt_mem: Memory/storage tools (vector stores, etc.)
      - agt_agt: Agent-to-agent communication (Task tool)
      - None: Skip monitoring (aiceberg-* tools to avoid recursion)
    """
    lowered = (tool_name or "").lower()
    if tool_name == "Task":
        return "agt_agt"
    if "aiceberg" in lowered:
        return None  # Don't monitor our own tools
    if lowered.startswith("mcp__") and any(token in lowered for token in MEM_PATTERNS):
        return "agt_mem"
    return "agt_tool"


def default_metadata(hook_name: str, data: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Build default metadata dict for Aiceberg events."""
    metadata: dict[str, Any] = {"user_id": user_id, "hook_event_name": hook_name}
    session_id = data.get("session_id")
    if session_id:
        metadata["caller_session_id"] = session_id
    return metadata


def build_create_payload(
    event_type: str,
    input_content: str,
    session_id: str,
    metadata: dict[str, Any],
    cfg: dict[str, Any],
    *,
    session_start: bool = False,
) -> dict[str, Any]:
    """
    Build CREATE payload for INPUT event.

    Why: Aiceberg requires CREATE before UPDATE (paired event model).
    CREATE returns event_id which is used in subsequent UPDATE.
    """
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


def build_update_payload(
    event_id: str,
    event_type: str,
    input_content: str,
    output_content: str,
    session_id: str,
    metadata: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Build UPDATE payload for OUTPUT event.

    Why: Completes the INPUT/OUTPUT pair for a previously created event_id.
    We intentionally send empty input on UPDATE and rely on event_id mapping
    to the original CREATE payload.
    """
    _ = input_content  # Kept in signature for compatibility with current call sites.
    return {
        "event_id": event_id,
        "event_type": event_type,
        "input": "",
        "output": output_content,
        "profile_id": cfg.get("profile_id", ""),
        "session_id": session_id or "",
        "use_case_id": cfg.get("use_case_id", ""),
        "forward_to_llm": bool(cfg.get("forward_to_llm", False)),
        "metadata": metadata,
    }


def emit_block_decision(hook_name: str, reason: str) -> dict[str, Any]:
    """
    Build block decision response for Claude runtime.

    Why: Different hooks expect different response formats:
      - PreToolUse: Needs permissionDecision field
      - PermissionRequest: Also needs permissionDecision
      - Others: Just decision + reason
    """
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


def policy_text(reason: str, max_chars: int) -> str:
    """Format policy violation text for OUTPUT events."""
    return cap_text(reason, max_chars)


# ============================================================================
# GENERIC HOOK SPECIFICATIONS
# ============================================================================


def generic_content_setup(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "Setup",
        "session_id": data.get("session_id", ""),
        "cwd": data.get("cwd", ""),
        "argv": data.get("argv", []),
    }


def generic_content_session_start(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "SessionStart",
        "session_id": data.get("session_id", ""),
        "source": data.get("source", ""),
        "resume": bool(data.get("resume", False)),
    }


def generic_content_notification(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "Notification",
        "session_id": data.get("session_id", ""),
        "message": data.get("message", ""),
        "level": data.get("level", ""),
    }


def generic_content_subagent_start(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "SubagentStart",
        "session_id": data.get("session_id", ""),
        "agent_id": data.get("agent_id", ""),
        "agent_type": data.get("agent_type", ""),
    }


def generic_content_teammate_idle(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "TeammateIdle",
        "session_id": data.get("session_id", ""),
        "teammate_id": data.get("teammate_id", ""),
        "idle_seconds": data.get("idle_seconds", 0),
    }


def generic_content_task_completed(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "TaskCompleted",
        "session_id": data.get("session_id", ""),
        "task_id": data.get("task_id", ""),
        "status": data.get("status", ""),
        "summary": data.get("summary", ""),
    }


def generic_content_config_change(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "ConfigChange",
        "session_id": data.get("session_id", ""),
        "changed_keys": data.get("changed_keys", []),
        "change_source": data.get("source", ""),
    }


def generic_content_worktree_create(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "WorktreeCreate",
        "session_id": data.get("session_id", ""),
        "worktree_path": data.get("worktree_path", ""),
        "branch": data.get("branch", ""),
    }


def generic_content_worktree_remove(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "WorktreeRemove",
        "session_id": data.get("session_id", ""),
        "worktree_path": data.get("worktree_path", ""),
    }


def generic_content_precompact(data: dict[str, Any]) -> Any:
    return {
        "hook_event_name": "PreCompact",
        "session_id": data.get("session_id", ""),
        "transcript_path": data.get("transcript_path", ""),
        "estimated_tokens": data.get("estimated_tokens", ""),
    }


# Generic hook specifications for telemetry and lifecycle events
GENERIC_HOOK_SPECS: dict[str, GenericHookSpec] = {
    "Setup": GenericHookSpec("agt_agt", "[setup_ack]", "setup", generic_content_setup),
    "SessionStart": GenericHookSpec("agt_agt", "[session_started]", "session_start", generic_content_session_start),
    "Notification": GenericHookSpec("agt_agt", "[notification_ack]", "notification", generic_content_notification),
    "SubagentStart": GenericHookSpec("agt_agt", "[subagent_started]", "subagent_start", generic_content_subagent_start),
    "TeammateIdle": GenericHookSpec("agt_agt", "[teammate_idle_seen]", "teammate_idle", generic_content_teammate_idle),
    "TaskCompleted": GenericHookSpec("agt_agt", "[task_completed_seen]", "task_completed", generic_content_task_completed),
    "ConfigChange": GenericHookSpec("agt_agt", "[config_change_seen]", "config_change", generic_content_config_change),
    "WorktreeCreate": GenericHookSpec("agt_agt", "[worktree_created]", "worktree_create", generic_content_worktree_create),
    "WorktreeRemove": GenericHookSpec("agt_agt", "[worktree_removed]", "worktree_remove", generic_content_worktree_remove),
    "PreCompact": GenericHookSpec("agt_agt", "[precompact_seen]", "precompact", generic_content_precompact),
}


def build_one_shot_content(content_obj: Any, fallback: dict[str, Any], max_chars: int) -> str:
    """Build content for one-shot events (CREATE+UPDATE in single flow)."""
    source = content_obj if content_obj is not None else fallback
    return normalize_text_payload(source, max_chars)


# ============================================================================
# NETWORK COMMUNICATION
# ============================================================================


def event_endpoint(cfg: dict[str, Any]) -> str:
    """Resolve the Aiceberg event API endpoint URL."""
    event_url = str(cfg.get("event_url", "")).strip()
    if event_url:
        return event_url
    base_url = str(cfg.get("base_url", "")).strip().rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/eap/v1/event"):
        return base_url
    return f"{base_url}/eap/v1/event"


def append_local_log(cfg: dict[str, Any], payload: dict[str, Any], response: dict[str, Any]) -> None:
    """
    Append event to local JSONL log file.

    Why: Provides local audit trail even when API is unreachable. Critical for
    debugging and ensuring no events are lost.
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
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(safe_json_dumps(entry) + "\n")
    except Exception as exc:
        log(f"warning: local log write failed: {exc}")


def print_payload(payload: dict[str, Any], *, mode: str) -> None:
    """Print payload to stderr for debugging."""
    log(f"{mode} payload: {safe_json_dumps(payload)}")


def print_response(response: dict[str, Any], *, mode: str) -> None:
    """Print response to stderr for debugging."""
    log(f"{mode} response: {safe_json_dumps(response)}")


def append_debug_trace(cfg: dict[str, Any], trace: dict[str, Any]) -> None:
    """Append debug trace entry to debug-trace.jsonl."""
    if not cfg.get("debug_trace", False):
        return
    path = os.path.realpath(str(cfg.get("debug_trace_path", "")))
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **trace}
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(safe_json_dumps(entry) + "\n")
    except Exception as exc:
        log(f"warning: debug trace write failed: {exc}")


def post_aiceberg(payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """
    POST payload to Aiceberg API via HTTP.

    Raises: urllib.error.HTTPError, urllib.error.URLError, TimeoutError on failure
    """
    url = event_endpoint(cfg)
    if not url:
        return {"event_result": "passed", "reason": "No endpoint configured (log-only mode)"}

    body = safe_json_dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": f"aiceberg-claude-hooks/{VERSION}",
    }
    if cfg.get("api_key"):
        headers["Authorization"] = str(cfg["api_key"])

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    # Allow insecure SSL for local testing
    context = None
    if os.environ.get("AICEBERG_INSECURE", "0") == "1":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    timeout = int(cfg.get("timeout_seconds", 15))
    with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}


def send_payload(payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Send payload to Aiceberg API (or mock/dry-run) and return response.

    Modes:
      - Normal: Send to API, return real response
      - Dry-run: Print payload, don't send, return mock "passed"
      - Mock: Local keyword matching, return "blocked" if keywords found
      - Disabled: Return "passed" immediately

    Why: Central point for all API communication, makes testing easier.
    Implements fail-open behavior (on error, return "passed" to avoid blocking user).
    """
    if cfg.get("print_payloads", False) or cfg.get("dry_run_no_send", False):
        print_payload(payload, mode="dry-run" if cfg.get("dry_run_no_send", False) else "send")

    if not cfg.get("enabled", True):
        response = {"event_result": "passed", "disabled": True}
        append_local_log(cfg, payload, response)
        if cfg.get("print_payloads", False) or cfg.get("dry_run_no_send", False):
            print_response(response, mode="disabled")
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
        append_local_log(cfg, payload, response)
        if cfg.get("print_payloads", False) or cfg.get("dry_run_no_send", False):
            print_response(response, mode="dry-run")
        return response

    if cfg.get("mock_mode", False):
        tokens_raw = str(cfg.get("mock_block_tokens", ""))
        tokens = [token.strip().lower() for token in tokens_raw.split(",") if token.strip()]
        is_update = bool(payload.get("event_id"))
        text = str(payload.get("output" if is_update else "input", ""))
        text_lower = text.lower()
        hit = next((token for token in tokens if token in text_lower), None)
        event_id = str(payload.get("event_id", "")).strip() if is_update else str(uuid.uuid4())
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
        append_local_log(cfg, payload, response)
        if cfg.get("print_payloads", False) or cfg.get("dry_run_no_send", False):
            print_response(response, mode="mock")
        return response

    # Real API call
    try:
        response = post_aiceberg(payload, cfg)
        append_local_log(cfg, payload, response)
        if cfg.get("print_payloads", False) or cfg.get("dry_run_no_send", False):
            print_response(response, mode="send")
        return response
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        error_message = f"HTTP {exc.code}: {body}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        error_message = f"Transport error: {exc}"
    except Exception as exc:
        error_message = f"Unexpected send error: {exc}"

    # Fail-open behavior: Return "passed" on error (don't block user if API is down)
    response = {"event_result": "passed", "error": error_message, "fail_open": True}
    if not cfg.get("fail_open", True):
        response = {"event_result": "block", "error": error_message, "reason": error_message}
    append_local_log(cfg, payload, response)
    if cfg.get("print_payloads", False) or cfg.get("dry_run_no_send", False):
        print_response(response, mode="error")
    return response
