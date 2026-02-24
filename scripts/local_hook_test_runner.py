#!/usr/bin/env python3
"""
Run local end-to-end tests for the hooks monitor without Cowork.

This runner uses monitor built-in mock mode (no network needed).
"""

import json
import os
import subprocess
import tempfile


def _root() -> str:
    return os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))


def _run_hook(event_name: str, payload: dict, env: dict) -> tuple[int, str, str]:
    cmd = [
        "python3",
        os.path.join(_root(), "scripts", "aiceberg_hooks_monitor.py"),
        "--event",
        event_name,
    ]
    proc = subprocess.run(
        cmd,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _write_demo_transcript() -> str:
    fd, path = tempfile.mkstemp(prefix="aiceberg-hook-demo-", suffix=".jsonl")
    os.close(fd)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "please help"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "safe answer"}]}},
    ]
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def main() -> int:
    plugin_root = _root()

    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = plugin_root
    env["AICEBERG_ENABLED"] = "true"
    env["AICEBERG_MODE"] = "enforce"
    env["AICEBERG_FAIL_OPEN"] = "true"
    env["AICEBERG_MOCK_MODE"] = "true"
    env["AICEBERG_MOCK_BLOCK_TOKENS"] = "jailbreak,toxic,malware,rm -rf /,[[block]]"
    env["AICEBERG_LOG_PATH"] = "/tmp/aiceberg-claude-hooks/local-test-events.jsonl"
    env["AICEBERG_DB_PATH"] = "/tmp/aiceberg-claude-hooks/local-test-monitor.db"

    cases = [
        (
            "setup",
            "Setup",
            {"hook_event_name": "Setup", "session_id": "s-local", "cwd": plugin_root, "argv": ["claude", "run"]},
        ),
        (
            "session_start",
            "SessionStart",
            {"hook_event_name": "SessionStart", "session_id": "s-local", "source": "new", "resume": False},
        ),
        (
            "safe_prompt",
            "UserPromptSubmit",
            {"hook_event_name": "UserPromptSubmit", "session_id": "s-local", "prompt": "hello"},
        ),
        (
            "blocked_prompt",
            "UserPromptSubmit",
            {"hook_event_name": "UserPromptSubmit", "session_id": "s-local", "prompt": "this is jailbreak content"},
        ),
        (
            "safe_tool_pre",
            "PreToolUse",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s-local",
                "tool_name": "Bash",
                "tool_use_id": "tool-1",
                "tool_input": {"command": "ls -la"},
            },
        ),
        (
            "permission_request",
            "PermissionRequest",
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "s-local",
                "tool_name": "Bash",
                "tool_input": {"command": "ls -la"},
                "permission_suggestions": [{"action": "allow_once"}],
            },
        ),
        (
            "safe_tool_post",
            "PostToolUse",
            {
                "hook_event_name": "PostToolUse",
                "session_id": "s-local",
                "tool_name": "Bash",
                "tool_use_id": "tool-1",
                "tool_response": "ok",
            },
        ),
        (
            "tool_failure",
            "PostToolUseFailure",
            {
                "hook_event_name": "PostToolUseFailure",
                "session_id": "s-local",
                "tool_name": "Bash",
                "tool_use_id": "tool-3",
                "error": "command failed",
                "is_interrupt": False,
            },
        ),
        (
            "blocked_tool_pre",
            "PreToolUse",
            {
                "hook_event_name": "PreToolUse",
                "session_id": "s-local",
                "tool_name": "Bash",
                "tool_use_id": "tool-2",
                "tool_input": {"command": "rm -rf /"},
            },
        ),
        (
            "notification",
            "Notification",
            {"hook_event_name": "Notification", "session_id": "s-local", "message": "build finished", "level": "info"},
        ),
        (
            "subagent_start",
            "SubagentStart",
            {"hook_event_name": "SubagentStart", "session_id": "s-local", "agent_id": "agent-1", "agent_type": "Task"},
        ),
    ]

    transcript = _write_demo_transcript()
    cases.append(
        (
            "stop_event",
            "Stop",
            {
                "hook_event_name": "Stop",
                "session_id": "s-local",
                "transcript_path": transcript,
                "stop_hook_active": False,
            },
        )
    )
    cases.append(
        (
            "subagent_stop",
            "SubagentStop",
            {
                "hook_event_name": "SubagentStop",
                "session_id": "s-local",
                "agent_id": "agent-1",
                "agent_transcript_path": transcript,
                "transcript_path": transcript,
                "stop_hook_active": False,
            },
        )
    )
    cases.extend(
        [
            (
                "teammate_idle",
                "TeammateIdle",
                {
                    "hook_event_name": "TeammateIdle",
                    "session_id": "s-local",
                    "teammate_id": "teammate-1",
                    "idle_seconds": 30,
                },
            ),
            (
                "task_completed",
                "TaskCompleted",
                {
                    "hook_event_name": "TaskCompleted",
                    "session_id": "s-local",
                    "task_id": "task-1",
                    "status": "success",
                    "summary": "Task done",
                },
            ),
            (
                "config_change",
                "ConfigChange",
                {
                    "hook_event_name": "ConfigChange",
                    "session_id": "s-local",
                    "changed_keys": ["model", "permission_mode"],
                    "source": "user",
                },
            ),
            (
                "worktree_create",
                "WorktreeCreate",
                {
                    "hook_event_name": "WorktreeCreate",
                    "session_id": "s-local",
                    "worktree_path": "/tmp/worktree-1",
                    "branch": "feature/test",
                },
            ),
            (
                "worktree_remove",
                "WorktreeRemove",
                {
                    "hook_event_name": "WorktreeRemove",
                    "session_id": "s-local",
                    "worktree_path": "/tmp/worktree-1",
                },
            ),
            (
                "precompact",
                "PreCompact",
                {
                    "hook_event_name": "PreCompact",
                    "session_id": "s-local",
                    "transcript_path": transcript,
                    "estimated_tokens": 2048,
                },
            ),
            (
                "session_end",
                "SessionEnd",
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": "s-local",
                },
            ),
        ]
    )

    print("Running local hook test cases...")
    all_ok = True
    for name, event, payload in cases:
        code, out, err = _run_hook(event, payload, env)
        print(f"\nCASE: {name}")
        print(f"  event: {event}")
        print(f"  exit: {code}")
        print(f"  stdout: {out or '(empty)'}")
        if err:
            print(f"  stderr: {err}")
        if code != 0:
            all_ok = False

    print("\nArtifacts:")
    print("  log: /tmp/aiceberg-claude-hooks/local-test-events.jsonl")
    print("  db:  /tmp/aiceberg-claude-hooks/local-test-monitor.db")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
