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

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from .api import (
    GENERIC_HOOK_SPECS,
    append_debug_trace,
    append_local_log,
    build_create_payload,
    build_one_shot_content,
    build_update_payload,
    classify_tool_event_type,
    default_metadata,
    emit_block_decision,
    policy_text,
    send_payload,
)
from .storage import (
    DEFAULT_DB_PATH,
    MAX_CONTENT_CHARS,
    OPEN_EVENT_TTL_SECONDS,
    HookEventEnvelope,
    SendOutcome,
    cap_text,
    clear_transcript_cursors_for_session,
    cleanup_stale,
    close_open_event,
    db_connect,
    drain_session_open_events,
    extract_last_llm_turn,
    extract_llm_turns,
    get_link,
    get_open_event,
    get_transcript_cursor,
    is_blocked,
    load_config,
    load_transcript_entries,
    log,
    normalize_text_payload,
    pop_link,
    reason_from_response,
    redact,
    safe_json_dumps,
    set_transcript_cursor,
    store_link,
    store_open_event,
    transcript_cursor_key,
)

# ============================================================================
# HOOK EVENT CLASSIFICATION
# ============================================================================

# Security-critical hooks sent to Aiceberg API immediately
# Why: These events have real-time security value for blocking/monitoring
SECURITY_CRITICAL_HOOKS = {
    "UserPromptSubmit",  # User input monitoring (can block jailbreak attempts)
    "PreToolUse",  # Tool execution control (can block dangerous commands)
    "PostToolUse",  # Tool output monitoring (can detect data exfiltration)
    "PostToolUseFailure",  # Tool error tracking (can identify attack patterns)
    "PermissionRequest",  # Permission mediation (access control)
    "Stop",  # Final response + LLM turns (output safety)
    "SubagentStop",  # Subagent LLM turns (nested agent safety)
}

# Lifecycle hooks that are typically telemetry-only
# Why: These don't contribute to security decisions, just operational visibility
TELEMETRY_ONLY_HOOKS = {
    "Setup",
    "SessionStart",
    "SessionEnd",
    "Notification",
    "TeammateIdle",
    "TaskCompleted",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
}

# Hooks that support explicit block decisions
# Why: Only these hooks allow returning {"decision": "block"} to stop execution
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

# Low-noise mode for focused debugging
TINY_DEBUG_HOOKS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SessionEnd",
}

# ============================================================================
# EVENT HANDLERS
# ============================================================================


def close_session_open_events_with_reason(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    session_id: str,
    reason: str,
) -> None:
    """
    Close all open events for a session with a policy violation message.

    Why: When blocking, we need to cleanly close all pending INPUT events
    (user prompt, tool calls, etc.) with an OUTPUT explaining the block.
    This ensures no orphaned events in Aiceberg dashboard.
    """
    block_text = policy_text(reason, int(cfg["max_content_chars"]))
    for event in drain_session_open_events(conn, session_id):
        send_payload(
            build_update_payload(
                event.event_id,
                event.event_type,
                event.input_content,
                block_text,
                event.session_id,
                event.metadata,
                cfg,
            ),
            cfg,
        )


def one_shot_event(
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
    """
    Handle a one-shot event (CREATE + UPDATE in single flow).

    Why: Used for events that don't have separate INPUT/OUTPUT timing
    (e.g., SessionEnd, Setup). We create and close them immediately.

    Telemetry filtering: If skip_telemetry_api_send=true and hook is telemetry-only,
    we log locally but don't send to API (saves costs, reduces dashboard clutter).
    """
    session_id = str(data.get("session_id", ""))
    user_id = str(cfg.get("default_user_id", "cowork_agent"))
    metadata = default_metadata(hook_name, data, user_id)
    if metadata_extra:
        metadata.update(metadata_extra)

    content = build_one_shot_content(content_obj, data, int(cfg["max_content_chars"]))

    # Telemetry filtering: Skip API for non-security events
    skip_api = cfg.get("skip_telemetry_api_send", True) and hook_name in TELEMETRY_ONLY_HOOKS
    if skip_api:
        create_payload = build_create_payload(event_type, content, session_id, metadata, cfg, session_start=False)
        update_payload = build_update_payload(
            "local-" + hook_name,
            event_type,
            content,
            output_text,
            session_id,
            metadata,
            cfg,
        )
        append_local_log(cfg, create_payload, {"event_result": "telemetry_skipped", "reason": "telemetry-only hook"})
        append_local_log(cfg, update_payload, {"event_result": "telemetry_skipped", "reason": "telemetry-only hook"})
        return {"event_result": "passed", "event_id": None, "telemetry_only": True}

    # Normal flow: Send to API
    create_payload = build_create_payload(event_type, content, session_id, metadata, cfg, session_start=False)
    create_response = send_payload(create_payload, cfg)
    event_id = str(create_response.get("event_id", "")).strip()
    if not event_id:
        return create_response

    store_open_event(conn, event_id, event_type, session_id, content, metadata)
    update_payload = build_update_payload(event_id, event_type, content, output_text, session_id, metadata, cfg)
    update_response = send_payload(update_payload, cfg)
    close_open_event(conn, event_id)
    if is_blocked(update_response):
        return update_response
    return create_response


def emit_transcript_llm_turns(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    hook_name: str,
    data: dict[str, Any],
    session_id: str,
    user_id: str,
    *,
    enforce: bool,
) -> dict[str, Any] | None:
    """
    Extract and emit LLM turns from transcript.

    Why: Claude doesn't expose BeforeModelCallEvent/AfterModelCallEvent hooks,
    so we reconstruct LLM thinking by parsing the transcript JSONL file.

    These are HISTORICAL events (already executed), not live. We can't block
    before the LLM runs - only detect violations after the fact.

    LLM Local-Only Mode: If llm_transcript_local_only=true, we log these locally
    but don't send to API yet (for testing/validation before enabling live sending).
    """
    transcript_path = str(data.get("transcript_path", "")).strip()
    if not transcript_path:
        return None

    entries = load_transcript_entries(transcript_path)
    turns = extract_llm_turns(entries)
    if not turns:
        return None

    # Cursor tracking: Only process new turns (avoid re-processing on subsequent Stop hooks)
    cursor_key = transcript_cursor_key(session_id, transcript_path)
    last_turn_index = get_transcript_cursor(conn, cursor_key)
    if last_turn_index >= len(turns):
        last_turn_index = -1
    start_turn = max(0, last_turn_index + 1)

    append_debug_trace(
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

    local_only = cfg.get("llm_transcript_local_only", True)

    for idx in range(start_turn, len(turns)):
        llm_input, llm_output = turns[idx]
        metadata = default_metadata(hook_name, data, user_id)
        metadata["source"] = "transcript_turn"
        metadata["transcript_path"] = transcript_path
        metadata["llm_turn_index"] = idx

        input_content = cap_text(llm_input, int(cfg["max_content_chars"]))
        output_content = cap_text(llm_output, int(cfg["max_content_chars"]))

        # LLM local-only mode: Log locally, don't send to API
        if local_only:
            create_payload = build_create_payload("agt_llm", input_content, session_id, metadata, cfg)
            update_payload = build_update_payload(
                f"local-llm-{idx}",
                "agt_llm",
                input_content,
                output_content,
                session_id,
                metadata,
                cfg,
            )
            append_local_log(
                cfg,
                create_payload,
                {"event_result": "llm_local_only", "reason": "transcript reconstruction (local-only mode)"},
            )
            append_local_log(
                cfg,
                update_payload,
                {"event_result": "llm_local_only", "reason": "transcript reconstruction (local-only mode)"},
            )
            set_transcript_cursor(conn, cursor_key, session_id, transcript_path, idx)
            continue

        # Normal flow: Send to API
        create_response = send_payload(build_create_payload("agt_llm", input_content, session_id, metadata, cfg), cfg)
        llm_event_id = str(create_response.get("event_id", "")).strip()
        if not llm_event_id:
            continue

        store_open_event(conn, llm_event_id, "agt_llm", session_id, input_content, metadata)
        update_response = send_payload(
            build_update_payload(
                llm_event_id,
                "agt_llm",
                input_content,
                output_content,
                session_id,
                metadata,
                cfg,
            ),
            cfg,
        )
        close_open_event(conn, llm_event_id)
        set_transcript_cursor(conn, cursor_key, session_id, transcript_path, idx)

        # Block if LLM output violates policy (only if enforcing and hook supports blocking)
        if enforce and is_blocked(update_response) and hook_name in BLOCK_CAPABLE_HOOKS:
            reason = reason_from_response(update_response, "LLM output blocked by Aiceberg policy.")
            close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return emit_block_decision(hook_name, reason)

    # Update cursor to prevent re-processing
    if start_turn >= len(turns):
        set_transcript_cursor(conn, cursor_key, session_id, transcript_path, len(turns) - 1)
    return None


def handle_generic_hook_with_spec(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    hook_name: str,
    data: dict[str, Any],
    *,
    enforce: bool,
    session_id: str,
) -> dict[str, Any] | None:
    """
    Handle generic/telemetry hooks using predefined specifications.

    Why: Many hooks (Setup, Notification, etc.) follow the same pattern.
    Instead of duplicating code, we use specifications to handle them uniformly.
    """
    spec = GENERIC_HOOK_SPECS.get(hook_name)
    if not spec:
        return None

    response = one_shot_event(
        conn,
        cfg,
        hook_name,
        data,
        event_type=spec.event_type,
        content_obj=spec.content_builder(data),
        output_text=spec.output_text,
        metadata_extra={"source": spec.source},
    )
    outcome = SendOutcome.from_response(response)
    if enforce and hook_name in BLOCK_CAPABLE_HOOKS and outcome.blocked:
        reason = outcome.reason or f"{hook_name} blocked by Aiceberg policy."
        close_session_open_events_with_reason(conn, cfg, session_id, reason)
        return emit_block_decision(hook_name, reason)
    return {}


def handle_hook_event(
    conn: sqlite3.Connection,
    cfg: dict[str, Any],
    hook_name: str,
    data: dict[str, Any],
) -> dict[str, Any]:
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

    # Tiny debug mode: Only process key hooks (reduces noise)
    if cfg.get("tiny_debug_mode", False) and hook_name not in TINY_DEBUG_HOOKS:
        append_debug_trace(
            cfg,
            {
                "phase": "skip",
                "reason": "tiny_debug_mode",
                "hook_event_name": hook_name,
                "session_id": session_id,
            },
        )
        return {}

    # ========================================================================
    # UserPromptSubmit - User asks a question
    # ========================================================================
    if hook_name == "UserPromptSubmit":
        prompt = str(data.get("prompt", data.get("user_prompt", "")))
        metadata = default_metadata(hook_name, data, user_id)
        metadata["source"] = "user_prompt_submit"
        create_payload = build_create_payload(
            "user_agt",
            cap_text(prompt, int(cfg["max_content_chars"])),
            session_id,
            metadata,
            cfg,
        )
        response = send_payload(create_payload, cfg)
        event_id = str(response.get("event_id", "")).strip()
        if event_id:
            store_open_event(conn, event_id, "user_agt", session_id, prompt, metadata)
            store_link(conn, f"user:{session_id}", event_id, session_id)
        if enforce and is_blocked(response):
            reason = reason_from_response(response, "User prompt blocked by Aiceberg policy.")
            close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return emit_block_decision(hook_name, reason)
        return {}

    # ========================================================================
    # PreToolUse - About to execute a tool
    # ========================================================================
    if hook_name == "PreToolUse":
        tool_name = str(data.get("tool_name", ""))
        event_type = classify_tool_event_type(tool_name)
        if not event_type:
            return {}  # Skip (e.g., aiceberg-* tools)
        tool_use_id = str(data.get("tool_use_id", ""))
        content_obj = {
            "tool_name": tool_name,
            "tool_input": data.get("tool_input", {}),
            "tool_use_id": tool_use_id,
        }
        content = normalize_text_payload(content_obj, int(cfg["max_content_chars"]))
        metadata = default_metadata(hook_name, data, user_id)
        metadata.update({"tool_name": tool_name, "tool_use_id": tool_use_id})
        response = send_payload(build_create_payload(event_type, content, session_id, metadata, cfg), cfg)
        event_id = str(response.get("event_id", "")).strip()
        if event_id:
            store_open_event(conn, event_id, event_type, session_id, content, metadata)
            if tool_use_id:
                store_link(conn, f"tool:{tool_use_id}", event_id, session_id)
        if enforce and is_blocked(response):
            reason = reason_from_response(response, "Tool call blocked by Aiceberg policy.")
            if event_id:
                send_payload(
                    build_update_payload(event_id, event_type, content, reason, session_id, metadata, cfg),
                    cfg,
                )
                close_open_event(conn, event_id)
            close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return emit_block_decision(hook_name, reason)
        return {}

    # ========================================================================
    # PostToolUse / PostToolUseFailure - Tool execution finished
    # ========================================================================
    if hook_name in {"PostToolUse", "PostToolUseFailure"}:
        tool_use_id = str(data.get("tool_use_id", ""))
        if not tool_use_id:
            return {}
        event_id = pop_link(conn, f"tool:{tool_use_id}")
        if not event_id:
            return {}
        open_event = get_open_event(conn, event_id)
        if not open_event:
            return {}

        if hook_name == "PostToolUseFailure":
            output = normalize_text_payload(
                {
                    "error": data.get("error", "unknown error"),
                    "is_interrupt": data.get("is_interrupt", False),
                },
                int(cfg["max_content_chars"]),
            )
        else:
            output = normalize_text_payload(data.get("tool_response", ""), int(cfg["max_content_chars"]))

        response = send_payload(
            build_update_payload(
                event_id,
                open_event.event_type,
                open_event.input_content,
                output,
                open_event.session_id,
                open_event.metadata,
                cfg,
            ),
            cfg,
        )
        close_open_event(conn, event_id)
        if enforce and hook_name == "PostToolUse" and is_blocked(response):
            reason = reason_from_response(response, "Tool result blocked by Aiceberg policy.")
            close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return emit_block_decision(hook_name, reason)
        return {}

    # ========================================================================
    # PermissionRequest - User permission prompt
    # ========================================================================
    if hook_name == "PermissionRequest":
        tool_name = str(data.get("tool_name", ""))
        event_type = classify_tool_event_type(tool_name) or "agt_tool"
        response = one_shot_event(
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
        if enforce and is_blocked(response):
            reason = reason_from_response(response, "Permission request blocked by Aiceberg policy.")
            close_session_open_events_with_reason(conn, cfg, session_id, reason)
            return emit_block_decision(hook_name, reason)
        return {}

    # ========================================================================
    # Stop - Conversation turn completed
    # ========================================================================
    if hook_name == "Stop":
        if bool(data.get("stop_hook_active", False)):
            return {}

        # Close the user_agt event with final LLM response
        user_event_id = get_link(conn, f"user:{session_id}")
        transcript_path = str(data.get("transcript_path", ""))
        _, llm_output = extract_last_llm_turn(transcript_path)

        if user_event_id:
            open_event = get_open_event(conn, user_event_id)
            if open_event:
                output = cap_text(llm_output or "No response", int(cfg["max_content_chars"]))
                response = send_payload(
                    build_update_payload(
                        user_event_id,
                        open_event.event_type,
                        open_event.input_content,
                        output,
                        open_event.session_id,
                        open_event.metadata,
                        cfg,
                    ),
                    cfg,
                )
                close_open_event(conn, user_event_id)
                if enforce and is_blocked(response):
                    reason = reason_from_response(response, "Final response blocked by Aiceberg policy.")
                    close_session_open_events_with_reason(conn, cfg, session_id, reason)
                    return emit_block_decision(hook_name, reason)

        # Process LLM turns from transcript (historical events)
        llm_decision = emit_transcript_llm_turns(conn, cfg, hook_name, data, session_id, user_id, enforce=enforce)
        if llm_decision:
            return llm_decision
        return {}

    # ========================================================================
    # SubagentStop - Subagent task completed
    # ========================================================================
    if hook_name == "SubagentStop":
        if bool(data.get("stop_hook_active", False)):
            return {}
        transcript_path = str(data.get("transcript_path", ""))
        llm_input, llm_output = extract_last_llm_turn(transcript_path)
        llm_decision = emit_transcript_llm_turns(conn, cfg, hook_name, data, session_id, user_id, enforce=enforce)
        if llm_decision:
            return llm_decision

        if llm_input or llm_output:
            response = one_shot_event(
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
            if enforce and is_blocked(response):
                reason = reason_from_response(response, "Subagent result blocked by Aiceberg policy.")
                close_session_open_events_with_reason(conn, cfg, session_id, reason)
                return emit_block_decision(hook_name, reason)
        return {}

    # ========================================================================
    # SessionEnd - Cleanup session state
    # ========================================================================
    if hook_name == "SessionEnd":
        for event in drain_session_open_events(conn, session_id):
            send_payload(
                build_update_payload(
                    event.event_id,
                    event.event_type,
                    event.input_content,
                    "[session_end]",
                    event.session_id,
                    event.metadata,
                    cfg,
                ),
                cfg,
            )
        clear_transcript_cursors_for_session(conn, session_id)
        one_shot_event(conn, cfg, hook_name, data, event_type="agt_agt", output_text="[session_closed]")
        return {}

    # ========================================================================
    # Generic hooks (Setup, Notification, etc.)
    # ========================================================================
    spec_response = handle_generic_hook_with_spec(
        conn,
        cfg,
        hook_name,
        data,
        enforce=enforce,
        session_id=session_id,
    )
    if spec_response is not None:
        return spec_response

    # ========================================================================
    # Fallback for unknown hooks
    # ========================================================================
    fallback_response = one_shot_event(
        conn,
        cfg,
        hook_name,
        data,
        event_type="agt_agt",
        metadata_extra={"source": "generic_hook"},
    )
    if enforce and hook_name in BLOCK_CAPABLE_HOOKS and is_blocked(fallback_response):
        reason = reason_from_response(fallback_response, f"{hook_name} blocked by Aiceberg policy.")
        close_session_open_events_with_reason(conn, cfg, session_id, reason)
        return emit_block_decision(hook_name, reason)
    return {}


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================


def parse_stdin_json() -> dict[str, Any]:
    """Parse JSON from stdin (Claude sends hook payload via stdin)."""
    raw = sys.stdin.read() or ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception as exc:
        log(f"bad stdin json: {exc}")
        return {}


def main() -> int:
    """
    Main entry point for Aiceberg Claude hooks monitor.

    Reads hook event from stdin, processes it, and returns decision to stdout.

    Exit codes:
      0: Success (always - errors logged but don't fail hook)
    """
    parser = argparse.ArgumentParser(description="Aiceberg Claude hook monitor")
    parser.add_argument("--event", default="", help="Hook event name override")
    args = parser.parse_args()

    data = parse_stdin_json()
    hook_name = args.event or str(data.get("hook_event_name", "")).strip()
    if not hook_name:
        log("warning: no hook_event_name provided")
        return 0

    cfg = load_config()
    max_chars = int(cfg.get("max_content_chars", MAX_CONTENT_CHARS))
    if max_chars <= 0:
        cfg["max_content_chars"] = MAX_CONTENT_CHARS

    conn = db_connect(str(cfg.get("db_path", DEFAULT_DB_PATH)))
    try:
        append_debug_trace(
            cfg,
            {
                "phase": "start",
                "hook_event_name": hook_name,
                "session_id": str(data.get("session_id", "")),
                "tiny_debug_mode": bool(cfg.get("tiny_debug_mode", False)),
            },
        )
        cleanup_stale(conn, OPEN_EVENT_TTL_SECONDS)
        if cfg.get("redact_secrets", True):
            preview = {
                "hook_event_name": hook_name,
                "session_id": data.get("session_id", ""),
                "payload": redact(data),
            }
            append_local_log(cfg, {"preview": preview}, {"event_result": "preview"})

        decision = handle_hook_event(conn, cfg, hook_name, data)
        append_debug_trace(
            cfg,
            {
                "phase": "end",
                "hook_event_name": hook_name,
                "session_id": str(data.get("session_id", "")),
                "decision": decision if decision else {},
            },
        )
        if decision:
            print(safe_json_dumps(decision))
    except Exception as exc:
        log(f"handler error ({hook_name}): {exc}")
        append_local_log(cfg, {"hook_event_name": hook_name, "payload": redact(data)}, {"error": str(exc)})
    finally:
        conn.close()

    return 0
