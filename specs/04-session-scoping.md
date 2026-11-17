# Spec 04: Session-Scoped Checkpoints

## Objective
Eliminate global state collisions caused by shared `memory_get_last` and `memory_update_last` pointers across concurrent agents.

## Storage Layout
Store session checkpoints as standard OKF v0.3 Markdown files inside `memory/sessions/<session_id>.md` with:
- `namespace: "sessions"`
- `type: "checkpoint"`
- `key: "<session_id>"` (validated against `^[a-z0-9]+(?:-[a-z0-9]+)*$`)

## MCP Tool Updates
1. **`memory_checkpoint_set(project_root: str, session_id: str, summary: str, active_keys: list[str], next_steps: list[str])`**
   - Creates or updates `memory/sessions/<session_id>.md` atomically.
   - Automatically manages internal `version` increment for that specific `session_id` file under per-session file lock.
2. **`memory_checkpoint_get(project_root: str, session_id: str)`**
   - Retrieves `memory/sessions/<session_id>.md`. Returns `null` if no checkpoint exists for that `session_id`.
3. **Backward-Compatible Deprecation (`memory_get_last`, `memory_update_last`)**
   - Add optional `session_id: str = "default"` parameter to both legacy tools.
   - Route both tools internally to `memory/sessions/<session_id>.md` and include a `"deprecation_notice"` field in the response recommending `memory_checkpoint_get` / `memory_checkpoint_set`.

## Acceptance Criteria (`tests/test_session_scoping.py`)
- Two concurrent agents writing checkpoints with `session_id="agent-a"` and `session_id="agent-b"` never overwrite or block each other.
- Legacy `memory_get_last()` calls without `session_id` continue to function using `"default"` without raising `TypeError`.