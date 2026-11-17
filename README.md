# MCP Project Memory

Local-first MCP server for durable agent memory. Git-tracked **OKF v0.3** Markdown under `memory/` is the Human-in-the-Loop (HITL) source of truth; SQLite WAL + FTS5 in `.mcp_memory/memories.db` is a derived search index.

---

## 1. Overview & Architecture

Agents and humans share one on-disk knowledge bundle:

| Layer | Path | Role |
| :--- | :--- | :--- |
| **HITL source of truth** | `memory/<namespace>/<key>.md` | Git-tracked Open Knowledge Format (OKF **v0.3**) Markdown with YAML frontmatter |
| **Derived index** | `.mcp_memory/memories.db` | SQLite with `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, FTS5 (`memories_fts`), and `file_hashes` for SHA-256 sync |
| **CAS locks** | `.mcp_memory/locks/<namespace>_<key>.lock` | Per-key `fcntl.flock` exclusive locks for concurrent `memory_store` |

**Hybrid pattern:** MCP tools write Markdown first (atomic temp + `fsync` + `os.replace`), then upsert SQLite in the same logical update. Humans may edit, add, or delete files under `memory/` directly (or via Git). `reconcile_memory_store()` re-hashes disk and syncs the index at server startup and before `memory_retrieve` / `memory_search`. Manual disk changes bypass CAS because disk is canonical for HITL audit.

Closed namespaces (always under `memory/<namespace>/`):

`discovery` · `architecture` · `contracts` · `deployment` · `sessions`

Default namespace for store/retrieve/delete is `discovery`.

**Environment variables** (optional; relative paths resolve against the project root):

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `MCP_MEMORY_PROJECT_ROOT` | caller cwd (set by `run.sh`) | Project root for relative paths |
| `MCP_MEMORY_DB_PATH` | `.mcp_memory/memories.db` | SQLite database path |
| `MCP_MEMORY_DIR` | `memory` | OKF Markdown bundle directory |

---

## 2. Installation & Project Setup

### 2.1 Clone and virtualenv

```bash
git clone <this-repo>
cd mcp-project-memory
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`run.sh` also creates `.venv` and installs deps on first launch, then execs `memory_server.py`.

### 2.2 Global MCP server registration

Register the server with your coding clients (Cursor, Claude Desktop, Antigravity, Windsurf, Codex, etc.):

```bash
.venv/bin/python setup.py
```

Useful flags:

| Flag | Effect |
| :--- | :--- |
| `--tool N` | Non-interactive tool choice (`1`–`8`; see wizard). Default when non-interactive: configure all (`8`) |
| `--non-interactive` | Skip prompts; use defaults |
| `--no-config-edit` | Print/skip config writes instead of editing client files |

The wizard points clients at this repo’s `run.sh`. Manual JSON shape:

```json
{
  "mcpServers": {
    "memory": {
      "command": "/ABSOLUTE/PATH/TO/run.sh"
    }
  }
}
```

Codex Desktop uses TOML (`~/.codex/config.toml`):

```toml
[mcp_servers.memory]
command = "/ABSOLUTE/PATH/TO/run.sh"
```

### 2.3 Downstream project scaffolding

Scaffold OKF v0.3 protocol files into an existing project:

```bash
.venv/bin/python setup.py --init-project <target_dir> [--force]
```

| Flag | Effect |
| :--- | :--- |
| `--init-project TARGET_DIR` | Non-interactive scaffold into `TARGET_DIR` |
| `--force` | Overwrite existing scaffolded files |

What gets installed:

| Artifact | Destination |
| :--- | :--- |
| Memory protocol rule | `<target>/.cursor/rules/memory-protocol.mdc` |
| Lifecycle skills | `<target>/.cursor/skills/{ingest-artifact,log-adr,triage-stale-memory}/SKILL.md` |
| Pre-commit validator | `<target>/scripts/validate_memory.py` (executable) |
| Namespace dirs + `.gitkeep` | `<target>/memory/{discovery,architecture,contracts,deployment,sessions}/` |
| Git pre-commit hook | If `<target>/.git/hooks` exists: ensures `python3 scripts/validate_memory.py` runs (creates or appends) |

Existing files are skipped unless `--force` is set. Hook status is reported as `created`, `appended`, or `unchanged`.

---

## 3. OKF v0.3 Schema Specification

Every record is Markdown with YAML frontmatter. On-disk path: `memory/<namespace>/<key>.md`. Filename stem must equal `key`.

### Required / documented frontmatter fields

| Field | Constraint |
| :--- | :--- |
| `schema_version` | Must be `"0.3"` |
| `key` | Single-segment kebab-case: `^[a-z0-9]+(?:-[a-z0-9]+)*$` |
| `namespace` | Closed set (below) |
| `type` | Closed set (below) |
| `version` | Integer `>= 1`; CAS increments by `+1` on each successful agent update |
| `updated_at` | ISO-8601 UTC (e.g. `2026-11-17T10:09:00Z`) |
| `updated_by` | `agent:<id>` or `human:<username>` |
| `status` | `draft` \| `active` \| `superseded` \| `deprecated` |
| `verified` | **Boolean**. Agents must not set `true` |
| `stale_after` | ISO date (`YYYY-MM-DD`); default is ~90 days ahead when omitted on store |
| `supersedes` | Optional key of replaced record, or omit / `null` |
| `tags` | List (may be empty) |
| `sources` | List (may be empty; strings or mappings) |

Optional display fields: `title`, `description`, `resource`.

### Closed namespaces

`discovery` · `architecture` · `contracts` · `deployment` · `sessions`

### Closed types

`story` · `constraint` · `adr` · `contract` · `runbook` · `checkpoint`

### Verified invariant

If `updated_by` starts with `agent:` and `verified` is `true`, serialization and validation fail (`Agents cannot set verified=true`). Only humans (or deterministic review tooling acting as `human:…`) may mark records verified. Agents should always write `verified: false`.

### Example

```markdown
---
schema_version: "0.3"
key: "adr-004-sqlite-wal-sync"
namespace: "architecture"
type: "adr"
version: 1
updated_at: "2026-11-17T10:09:00Z"
updated_by: "agent:cursor-architect"
status: "active"
verified: false
stale_after: "2027-02-17"
supersedes: null
tags: ["storage", "concurrency", "sqlite"]
sources: ["docs/prd-v1.md", "commit:8f9a2c1"]
---

## Context
…

## Decision / Specification
…

## Consequences & Trade-offs
…
```

---

## 4. Concurrency & Sync Guarantees

### 4.1 Strict CAS (`expected_version`)

`memory_store` requires optimistic concurrency via `expected_version: int = 0`:

| Call | Meaning |
| :--- | :--- |
| `expected_version=0` | Create only — fails if `memory/<namespace>/<key>.md` already exists |
| `expected_version=<current>` | Update — must equal the integer `version` currently on disk; success writes `version = current + 1` |

**Algorithm (per key):**

1. Acquire exclusive `fcntl.flock` on `.mcp_memory/locks/<namespace>_<key>.lock`.
2. Compare `expected_version` to disk (missing file ⇒ actual `0`).
3. On mismatch, raise `VersionConflictError` (lock released).
4. On match, serialize OKF, **atomic write** (temp file in same directory → flush → `os.fsync` → `os.replace`), then upsert SQLite + `file_hashes` in one connection.

**Structured conflict response** (`memory_store` returns a JSON string):

```json
{
  "error_code": "VERSION_CONFLICT",
  "key": "<key>",
  "expected_version": 1,
  "actual_version": 2,
  "current_record": "<full YAML frontmatter + Markdown body on disk>"
}
```

Caller should re-read (or use `current_record`), merge, and retry with the new `actual_version`.

### 4.2 Bidirectional SHA-256 hash sync

`file_hashes` tracks `(rel_path, sha256, version, last_synced_at)` for paths like `architecture/adr-004-sqlite-wal-sync.md`.

`reconcile_memory_store()` runs:

- at process startup (`init_db()` then reconcile in `memory_server.py`), and
- at the start of every `memory_retrieve` and `memory_search` (scoped to the call’s `project_root`).

**Reconciliation:**

1. Scan `memory/**/*.md` (skips `memory/index.md`); SHA-256 each file’s raw UTF-8 bytes.
2. **Deleted on disk** → remove from `memories` / FTS / `file_hashes`.
3. **Added or modified** (missing hash or digest mismatch) → parse OKF; invalid YAML / non-conforming frontmatter → structured warning on **stderr**, skip file, do not crash.
4. Regenerate `memory/index.md` only if at least one add/modify/delete occurred.
5. Unchanged digests incur no SQLite write for that file.

Human/Git edits ingested this way **do not** go through `expected_version` CAS.

---

## 5. MCP Tool Reference

All tools return `str`. Every tool requires a non-empty `project_root` (absolute path to the active project). Missing `project_root` returns an English error string.

Default namespace for CRUD tools: `"discovery"`.

### `memory_store`

Create or update an OKF v0.3 record under `memory/<namespace>/<key>.md`.

| Parameter | Type | Default | Notes |
| :--- | :--- | :--- | :--- |
| `key` | `str` | — | Required. Single-segment kebab-case |
| `content` | `str` \| `dict` | — | Required. Body text or structured content |
| `project_root` | `str` | — | Required |
| `tags` | `list[str]` \| omit | `None` | Stored as YAML list |
| `namespace` | `str` | `"discovery"` | Closed namespace set |
| `concept_type` | `str` \| omit | `None` → `"story"` | Maps to frontmatter `type` |
| `title` | `str` \| omit | — | Optional |
| `description` | `str` \| omit | — | Optional |
| `resource` | `str` \| omit | — | Optional URI |
| `status` | `str` \| omit | `None` → `"active"` | Closed status set |
| `stale_after` | `str` \| omit | ~90 days ahead | ISO date |
| `sources` | `list` \| omit | — | Provenance |
| `usage_window` | `dict` \| omit | — | Accepted for compatibility; unused in v0.3 |
| `verified` | `bool` \| omit | `false` | Agents must not pass `true` |
| `generated_by` | `str` \| omit | — | Legacy alias for `updated_by` |
| `updated_by` | `str` \| omit | `"agent:mcp-memory"` | `agent:…` or `human:…` |
| `version` | `int` \| omit | — | Ignored for CAS; version comes from disk / `expected_version` |
| `expected_version` | `int` | `0` | Strict CAS precondition |
| `supersedes` | `str` \| omit | — | Optional predecessor key |
| `runtime` / `parameters` / `executor` / `attester` / `computation` | various | — | Accepted for compatibility; unused in v0.3 |

**Success:** confirmation + file path + full OKF payload.  
**Conflict:** JSON with `error_code: "VERSION_CONFLICT"` (see §4.1).

### `memory_retrieve`

| Parameter | Type | Default |
| :--- | :--- | :--- |
| `key` | `str` | — (required) |
| `project_root` | `str` | — (required) |
| `namespace` | `str` | `"discovery"` |

Reconciles disk → SQLite first. **Success:** full OKF Markdown (`raw_payload`). **Miss:** not-found English string.

### `memory_search`

| Parameter | Type | Default |
| :--- | :--- | :--- |
| `project_root` | `str` | — (required) |
| `query` | `str` \| omit | `None` (FTS5 / LIKE fallback) |
| `tags` | `list[str]` \| omit | `None` |
| `namespace` | `str` \| omit | `None` (all namespaces) |
| `limit` | `int` | `10` |

Reconciles first. **Success:** numbered summary with key, namespace, tags, `updated_at`, and fenced OKF payload. **Miss:** `"No matching memories found."`

### `memory_delete`

| Parameter | Type | Default |
| :--- | :--- | :--- |
| `key` | `str` | — (required) |
| `project_root` | `str` | — (required) |
| `namespace` | `str` | `"discovery"` |

Removes the SQLite row and the on-disk `.md` file. Success / not-found English strings.

### `memory_checkpoint_set`

Session-scoped checkpoint at `memory/sessions/<session_id>.md` (`namespace: sessions`, `type: checkpoint`, `key: <session_id>`). Manages per-session lock and internal version bump.

| Parameter | Type | Default |
| :--- | :--- | :--- |
| `project_root` | `str` | — (required) |
| `session_id` | `str` | — (required; kebab-case) |
| `summary` | `str` | — (required) |
| `active_keys` | `list[str]` \| omit | `[]` |
| `next_steps` | `list[str]` \| omit | `[]` |

**Success:** confirmation + OKF payload. Invalid `session_id` → JSON `INVALID_SESSION_ID`. CAS failure → JSON `VERSION_CONFLICT`.

### `memory_checkpoint_get`

| Parameter | Type | Default |
| :--- | :--- | :--- |
| `project_root` | `str` | — (required) |
| `session_id` | `str` | — (required) |

**Hit:** raw OKF Markdown. **Miss:** JSON `null` (string `"null"`). Invalid id → JSON `INVALID_SESSION_ID`.

### Deprecated: `memory_get_last` / `memory_update_last`

Prefer `memory_checkpoint_get` / `memory_checkpoint_set`. Both legacy tools route to `memory/sessions/<session_id>.md` and include a `deprecation_notice` in the response text.

**`memory_get_last`**

| Parameter | Type | Default |
| :--- | :--- | :--- |
| `project_root` | `str` | — (required) |
| `namespace` | `str` | `"discovery"` | Kept for call compatibility; **unused** for routing (storage is always `sessions`) |
| `session_id` | `str` | `"default"` |

**`memory_update_last`**

| Parameter | Type | Default |
| :--- | :--- | :--- |
| `content` | `str` \| `dict` | — (required) |
| `project_root` | `str` | — (required) |
| `namespace` | `str` | `"discovery"` | Unused for routing |
| `summary` | `str` \| omit | `None` |
| `session_id` | `str` | `"default"` |

Dict `content` may supply `summary`, `active_keys`, and `next_steps`; otherwise content is treated as summary / next-step text.

---

## 6. Downstream Cursor Skills & Testing

### Bundled lifecycle skills

Copied by `--init-project` into the target’s `.cursor/skills/`:

| Skill | Purpose |
| :--- | :--- |
| `ingest-artifact` | Distill a PRD, interview, API spec, or runbook into atomic OKF records via `memory_store` (dedupe with `memory_search` first; always `verified: false`) |
| `log-adr` | Record or supersede architecture decisions in `namespace: architecture`, `type: adr`, with Context / Decision / Consequences body sections and CAS updates |
| `triage-stale-memory` | HITL review of records that are past `stale_after` or `verified: false` (excludes `memory/sessions/`); wait for human confirmation before verify / refresh / deprecate |

The always-on rule `memory-protocol.mdc` requires reading `memory/index.md`, searching before design/code, strict CAS on updates, and session-scoped checkpoints.

### Test suite

```bash
.venv/bin/python -W default -m pytest -v
```

Covers OKF v0.3 schema, CAS concurrency, bidirectional hash sync, session scoping, and project init scaffolding (`tests/`). Runtime deps plus `pytest` are listed in `requirements.txt`.
