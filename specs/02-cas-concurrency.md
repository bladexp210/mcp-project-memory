# Spec 02: Optimistic Concurrency Control (CAS)

## Objective
Prevent silent lost-update race conditions when multiple Cursor agents write to the same OKF v0.3 memory record concurrently.

## Interface Changes (`memory_store`)
Add an integer parameter `expected_version: int = 0` to `memory_store` (`0` asserts the record does not exist yet; `>= 1` asserts the exact current disk version).

## Execution Algorithm
1. Acquire a cross-process file lock (`fcntl.flock` or `portalocker`) on `.mcp_memory/locks/<namespace>_<key>.lock`.
2. Check if `memory/<namespace>/<key>.md` exists on disk:
   - **File does not exist:**
     - If `expected_version != 0`, release lock and raise `VersionConflictError(key, expected=expected_version, actual=0)`.
     - Set new record `version = 1`.
   - **File exists:**
     - Parse current disk frontmatter to read `actual_version = int(meta["version"])`.
     - If `expected_version != actual_version`, release lock and raise `VersionConflictError(key, expected=expected_version, actual=actual_version)`.
     - Set new record `version = actual_version + 1`.

## Error Payload (`VersionConflictError`)
When `VersionConflictError` occurs, `memory_store` must return a structured error response containing:
- `error_code`: `"VERSION_CONFLICT"`
- `key`: Target record key
- `expected_version`: Value passed by caller
- `actual_version`: Current integer version on disk
- `current_record`: Full YAML frontmatter and Markdown body currently on disk so the calling agent can merge and retry.

## Acceptance Criteria (`tests/test_cas_concurrency.py`)
- Two threads calling `memory_store` simultaneously with `expected_version=1` result in exactly one success (`version=2`) and one `VERSION_CONFLICT` error.
- Creating an existing key with `expected_version=0` fails with `VERSION_CONFLICT`.
- No partial or truncated `.md` files occur under 10-thread concurrent stress testing.