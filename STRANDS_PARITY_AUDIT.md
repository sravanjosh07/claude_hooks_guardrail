# Strands vs Claude Hooks Parity Audit

This document compares the Strands monitoring design in:

- `/Users/sravanjosh/Documents/Aiceberg/ab_strands_samples/src/ab_strands_samples/aiceberg_monitor.py`

with the Claude hooks monitor in:

- `/Users/sravanjosh/Documents/Aiceberg/Claude_agent_new/aiceberg-claude-hooks-guardrails/scripts/aiceberg_hooks_monitor.py`

## 1) Architectural Equivalence

| Concern | Strands monitor | Claude hooks monitor |
|---|---|---|
| Input/output pairing | In-memory `OpenEvent` map per turn | SQLite `open_events` table across hook subprocesses |
| Linking tool input -> output | `tool_event_ids` map | SQLite `links` table (`tool:<tool_use_id>`) |
| Block handling | `_handle_block()` closes all open events, raises exception | Block decision returned to hook + closes all open events with policy output |
| Session continuity | `session_id` in handler instance | `session_id` from hook payload + persisted DB state |
| LLM observability | Native `BeforeModelCallEvent`/`AfterModelCallEvent` | Transcript reconstruction at `Stop`/`SubagentStop` |

## 2) Event Boundary Mapping

| Strands event | Claude hook equivalent | Aiceberg event type | Blockable |
|---|---|---|---|
| `MessageAddedEvent` (user) | `UserPromptSubmit` | `user_agt` create | Yes |
| `BeforeToolCallEvent` | `PreToolUse` | `agt_tool`/`agt_mem`/`agt_agt` create | Yes |
| `AfterToolCallEvent` | `PostToolUse`/`PostToolUseFailure` | linked update | `PostToolUse`: Yes, `PostToolUseFailure`: No |
| `BeforeModelCallEvent` | Not directly exposed | N/A | No |
| `AfterModelCallEvent` | Not directly exposed | N/A | No |
| `AfterInvocationEvent` | `Stop` | `user_agt` update + `agt_llm` reconstructed turns | Yes (at boundary) |
| N/A in Strands | `PermissionRequest` | one-shot `agt_tool`/`agt_agt` | Yes |
| N/A in Strands | `SubagentStart`/`SubagentStop` | `agt_agt` + transcript-derived `agt_llm` | `SubagentStop`: Yes |
| N/A in Strands | `SessionStart`/`SessionEnd`/`Setup`/`Notification`/`Worktree*`/`PreCompact` | `agt_agt` telemetry | No |

## 3) Data Model Parity

Strands dataclasses:

- `OpenEvent`
- `TurnState`
- `FlowLogger` (in `flow_logger.py`)

Claude hooks dataclasses (added):

- `HookEventEnvelope`
- `OpenEventRecord`
- `SendOutcome`
- `GenericHookSpec`

These provide the same explicit modeling style while supporting subprocess-based hook execution.

## 4) Detailed Claude Hook Coverage

| Hook | Content sent | Metadata source | Output text |
|---|---|---|---|
| `Setup` | cwd/argv/session basics | `setup` | `[setup_ack]` |
| `SessionStart` | source/resume/session | `session_start` | `[session_started]` |
| `UserPromptSubmit` | user prompt | `user_prompt_submit` | (update later at `Stop`) |
| `PreToolUse` | tool name/input/id | `PreToolUse` | (update later in `PostToolUse*`) |
| `PermissionRequest` | tool + permission suggestions | `permission_request` | `[permission_reviewed]` |
| `PostToolUse` | tool output | inherited from linked input | linked update |
| `PostToolUseFailure` | error + interrupt flag | inherited from linked input | linked update |
| `Notification` | level/message/session | `notification` | `[notification_ack]` |
| `SubagentStart` | agent id/type | `subagent_start` | `[subagent_started]` |
| `SubagentStop` | transcript-derived LLM turns + summary | `transcript_turn` | `[subagent_stop_captured]` |
| `Stop` | close `user_agt` + transcript-derived LLM turns | `transcript_turn` | linked updates |
| `TeammateIdle` | teammate id/idle seconds | `teammate_idle` | `[teammate_idle_seen]` |
| `TaskCompleted` | task id/status/summary | `task_completed` | `[task_completed_seen]` |
| `ConfigChange` | changed keys/source | `config_change` | `[config_change_seen]` |
| `WorktreeCreate` | path/branch | `worktree_create` | `[worktree_created]` |
| `WorktreeRemove` | path | `worktree_remove` | `[worktree_removed]` |
| `PreCompact` | transcript path/token estimate | `precompact` | `[precompact_seen]` |
| `SessionEnd` | session close signal | `SessionEnd` | `[session_closed]` + closes remaining open events |

## 5) LLM Calls: Exact Behavior

Claude does not expose direct pre/post model-call hooks.

Current implementation:

1. Read `transcript_path` at `Stop`/`SubagentStop`.
2. Split transcript into assistant turns.
3. Emit one `agt_llm` create/update pair per newly observed turn.
4. Use SQLite transcript cursor (`transcript_cursors`) to avoid duplicates.
5. If policy blocks at these boundaries, return hook `decision:block`.

This gives strong observability plus boundary enforcement, but not pre-generation interception.

## 6) Invariant: Every Input Gets Closed

Parity rule from Strands is implemented:

- On block decisions, open events for the session are force-closed with policy output.
- On normal completion, events close in their natural hook pair.
- On `SessionEnd`, leftovers close with `[session_end]`.

This keeps Aiceberg timelines consistent and avoids orphaned input events.

