# Spec 00: Master Architecture (Read-Only Audit Baseline)

**Audit date:** 2026-11-17  
**Scope:** Current `mcp-project-memory` fork as-is. No Python, test, or template mutations in this pass.  
**Upstream docs:** `SPEC.md` / `OKF_RULES.md` (OKF **v0.2**). Fork targets OKF **v0.3** via `specs/01`–`04` + `templates/`.

---

## 1. Repository inventory

### 1.1 Root layout

| Path | Role |
| :--- | :--- |
| `memory_server.py` | FastMCP entrypoint; registers all MCP tools; calls `init_db()` at import |
| `db.py` | SQLite schema, FTS5, CRUD (`store_memory`, `retrieve_memory`, `search_memories`, `delete_memory`) |
| `okf_engine.py` | OKF v0.2 serialize/parse/validate; disk path resolution; index/log sync |
| `test_memory.py` | Entire existing test suite (`unittest`); **not** under `tests/` |
| `setup.py` | Interactive MCP client config wizard (not a setuptools package) |
| `run.sh` | Creates `.venv`, installs `requirements.txt`, execs `memory_server.py` |
| `requirements.txt` | Runtime deps only |
| `SPEC.md` | Full OKF **v0.2** specification (copied upstream) |
| `OKF_RULES.md` | Agent-oriented OKF v0.2 rules distill |
| `README.md` | Product docs; documents six MCP tools |
| `LICENSE` | Project license |
| `glama.json` | Glama MCP registry metadata |
| `.gitignore` | Ignores `.venv/`, `.mcp_memory/`, `memory/`, `*.db`, etc. |
| `specs/` | Fork implementation specs (`01`–`04`; this file is `00`) |
| `templates/` | Downstream project templates (**do not edit** unless instructed) |
| `tests/` | **Empty** placeholder (future pytest home per guardrails) |
| `memory/` | Empty runtime store dir (gitignored content) |
| `scripts/` | Empty placeholder |
| `.cursor/rules/00-fork-guardrails.mdc` | Spec-driven workflow + storage invariants |

### 1.2 Templates (downstream only — boundary protected)

| Path | Role |
| :--- | :--- |
| `templates/.cursor/rules/memory-protocol.mdc` | OKF v0.3 agent protocol (CAS, sessions, verified rules) |
| `templates/scripts/validate_memory.py` | Pre-commit OKF **v0.3** frontmatter validator |

### 1.3 Explicit non-source

`.git/`, `.obsidian/`, `.claude/`, `.cursor/skills/`, `.cursor/references/` are tooling/editor artifacts, not runtime modules.

---

## 2. Module map, entry points, dependencies

### 2.1 Runtime call graph

```
run.sh
  └─ .venv/bin/python memory_server.py
        ├─ FastMCP("MCP-Memory")  → mcp.run() under __main__
        ├─ init_db()              → db.get_connection / schema
        └─ @mcp.tool handlers
              └─ db.store_memory | retrieve_memory | search_memories | delete_memory
                    └─ okf_engine.serialize_okf | parse_okf | sync_memory_to_disk |
                       remove_memory_from_disk | get_memory_file_path |
                       update_directory_index | append_log_entry
```

### 2.2 Module → public functions

| Module | Functions |
| :--- | :--- |
| `memory_server.py` | `memory_get_last`, `memory_update_last`, `memory_store`, `memory_retrieve`, `memory_search`, `memory_delete` |
| `db.py` | `get_project_root`, `get_default_db_path`, `get_default_memories_dir`, `get_connection`, `init_db`, `store_memory`, `retrieve_memory`, `search_memories`, `delete_memory` |
| `okf_engine.py` | `_get_utc_now_iso`, `serialize_okf`, `parse_okf`, `validate_okf_conformance`, `get_memory_file_path`, `update_directory_index`, `append_log_entry`, `sync_memory_to_disk`, `remove_memory_from_disk` |

### 2.3 Entry points

| Entry | How invoked |
| :--- | :--- |
| MCP server | `./run.sh` or client `command` → `memory_server.py` → `mcp.run()` |
| Setup wizard | `python3 setup.py` |
| Tests (current) | `python3 test_memory.py` or `pytest test_memory.py` |
| Tests (target per guardrails) | `pytest` under `tests/` (not yet populated) |

### 2.4 Dependency list

**Declared in `requirements.txt`:**

| Package | Constraint | Installed in audit `.venv` |
| :--- | :--- | :--- |
| `fastmcp` | `<=3` | `3.0.0` (pulls `mcp 1.30.0`) |
| `pyyaml` | `>=6.0` | `6.0.3` |

**Stdlib used (no pin):** `sqlite3`, `json`, `os`, `pathlib`, `datetime`, `typing`, `tempfile` (tests), `unittest` (tests), `warnings`.

**Missing relative to specs / templates / guardrails:**

| Dependency / capability | Needed by | Present? |
| :--- | :--- | :--- |
| `PyYAML` | `okf_engine`, template validator | Yes (`pyyaml`) |
| `portalocker` | Spec 02 (optional alt to `fcntl`) | **No** — not in `requirements.txt` |
| `fcntl` | Spec 02 file locks | Stdlib on Linux; **not portable** to Windows |
| `pytest` | Guardrails (“pytest exits 0”) | **Not** in `requirements.txt`; used only for this audit |
| Atomic write helpers | Guardrails + Spec 02 | **Not implemented** (`open`+`write` only) |

**Environment variables (runtime):**

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `MCP_MEMORY_PROJECT_ROOT` | caller cwd (via `run.sh`) | Project root for relative paths |
| `MCP_MEMORY_DB_PATH` | `.mcp_memory/memories.db` | SQLite path (abs or relative to root) |
| `MCP_MEMORY_DIR` | `memory` | Markdown bundle directory |

**On-disk layout (per project root):**

```
<project_root>/
  memory/                          # OKF markdown bundle
    <key/segments>.md              # namespace == "default"
    <namespace>/<key/segments>.md  # namespace != "default"
    index.md / log.md              # generated
  .mcp_memory/
    memories.db                    # SQLite + WAL sidecars
    locks/                         # Spec 02 target (does not exist yet)
```

---

## 3. SQLite schema, pragmas, FTS5, index paths

### 3.1 Connection factory (`db.get_connection`)

- Opens `sqlite3.connect(abs_path)`, sets `row_factory = sqlite3.Row`.
- Creates parent directory of the DB file if missing.
- **Does not** set `PRAGMA busy_timeout`.
- Context-manager usage commits/rollbacks but **does not close** the connection (Python `sqlite3` behavior) → baseline `ResourceWarning: unclosed database` under `-W default`.

### 3.2 Pragmas (`db.init_db`)

| Pragma | Current | Guardrail / Spec target |
| :--- | :--- | :--- |
| `journal_mode=WAL` | **Yes** | Required |
| `busy_timeout=5000` | **Missing** | Required by `00-fork-guardrails.mdc` |

### 3.3 Tables

**`memories`** (primary key `(key, namespace)`):

| Column | Type | Notes |
| :--- | :--- | :--- |
| `key` | `TEXT NOT NULL` | Part of PK |
| `namespace` | `TEXT NOT NULL DEFAULT 'default'` | Part of PK |
| `okf_payload` | `TEXT NOT NULL` | Full Markdown+YAML document |
| `tags` | `TEXT NOT NULL DEFAULT '[]'` | JSON array string |
| `created_at` | `DATETIME DEFAULT CURRENT_TIMESTAMP` | Preserved on upsert |
| `updated_at` | `DATETIME DEFAULT CURRENT_TIMESTAMP` | Refreshed on upsert |

**Absent today (Spec 03):**

- No `file_hashes` table.
- No `content_hash` / `sha256` / `rel_path` / `last_synced_at` columns on `memories`.
- No integer `version` column (version will live in YAML frontmatter per Specs 01–02).

### 3.4 FTS5

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    key UNINDEXED,
    namespace UNINDEXED,
    tags,
    okf_payload
);
```

**Triggers:** `memories_ai` (INSERT), `memories_ad` (DELETE), `memories_au` (UPDATE) keep `memories_fts` aligned by `rowid`.

**Search path (`search_memories`):**

1. Optional `namespace = ?` filter.
2. If `query`: FTS5 `MATCH` with prefix terms `"term"*`; `ORDER BY rank`; fetch `limit * 2` then filter tags in Python.
3. On `sqlite3.OperationalError`: LIKE fallback on `key` / `okf_payload`.
4. If no query: `ORDER BY updated_at DESC LIMIT ?`.

### 3.5 Index paths (filesystem)

| Concept | Resolution (`get_memory_file_path`) |
| :--- | :--- |
| Key | Path segments; auto-appends `.md`; rejects empty, `.`/`..`, escape outside `base_dir` |
| Namespace `default` | `{base_dir}/{key}.md` (**no** `default/` folder) |
| Other namespace | `{base_dir}/{namespace}/{key}.md` (single segment only) |
| Bundle indexes | `update_directory_index` walks parents to bundle root; root `index.md` gets `okf_version: "0.2"` |
| Bundle log | `append_log_entry` → `{base_dir}/log.md` |
| Writes | **Non-atomic:** `open(path, "w")` then write (no temp/`fsync`/`os.replace`) |

---

## 4. Registered MCP tools (exact surface)

All tools return **`str`**. Errors are plain English strings (`"Error: ..."` / `"Error storing memory: ..."`), not structured JSON with `error_code`. Every tool requires non-empty `project_root` or returns a required-field error string.

### 4.1 `memory_store`

```text
memory_store(
  key: str,
  content: Union[str, Dict[str, Any]],
  project_root: str,
  tags: Optional[List[str]] = None,
  namespace: str = "default",
  concept_type: Optional[str] = None,
  title: Optional[str] = None,
  description: Optional[str] = None,
  resource: Optional[str] = None,
  status: Optional[str] = None,
  stale_after: Optional[str] = None,
  sources: Optional[List[Dict[str, Any]]] = None,
  usage_window: Optional[Dict[str, str]] = None,
  verified: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
  generated_by: Optional[str] = None,
  runtime: Optional[str] = None,
  parameters: Optional[List[Dict[str, Any]]] = None,
  executor: Optional[Dict[str, Any]] = None,
  attester: Optional[Dict[str, Any]] = None,
  computation: Optional[str] = None,
) -> str
```

**Success return (string):**  
`Successfully stored memory for key '{key}' in namespace '{namespace}'.\nFile synced to: {file_path}\n\nOKF Record Payload:\n{okf_payload}`

**Underlying `db.store_memory` return dict:**  
`{key, namespace, tags, created_at, updated_at, okf_payload, file_path}`

**Missing vs Spec 02:** no `expected_version` parameter.

### 4.2 `memory_retrieve`

```text
memory_retrieve(key: str, project_root: str, namespace: str = "default") -> str
```

**Success:** `record["raw_payload"]` (full OKF markdown).  
**Miss:** `"Memory record with key '{key}' not found in namespace '{namespace}'."`

### 4.3 `memory_search`

```text
memory_search(
  project_root: str,
  query: Optional[str] = None,
  tags: Optional[List[str]] = None,
  namespace: Optional[str] = None,
  limit: int = 10,
) -> str
```

**Success:** multi-line summary with count, key, namespace, tags, updated_at, fenced `okf_payload`.  
**Miss:** `"No matching memories found."`

### 4.4 `memory_delete`

```text
memory_delete(key: str, project_root: str, namespace: str = "default") -> str
```

**Success / miss:** English confirmation or not-found string. Bool from `db.delete_memory`.

### 4.5 `memory_get_last`

```text
memory_get_last(project_root: str, namespace: str = "default") -> str
```

Hard-coded retrieve of key `system/last_memory`.  
**Miss:** directive string to start fresh.  
**Hit:** header + `raw_payload`.

### 4.6 `memory_update_last`

```text
memory_update_last(
  content: Union[str, Dict[str, Any]],
  project_root: str,
  namespace: str = "default",
  summary: Optional[str] = None,
) -> str
```

Calls `store_memory` with fixed key `system/last_memory`, tags `["last_memory","checkpoint","milestone"]`, `concept_type="Checkpoint"`, etc.

**Missing vs Spec 04:** no `session_id`; no `memory_checkpoint_get` / `memory_checkpoint_set` tools.

---

## 5. Current OKF serialization vs Spec 01 (v0.3)

### 5.1 What `serialize_okf` emits today (v0.2-shaped)

Always / typically present:

- `type` (from `concept_type`, default `"Agent Memory"`)
- `title`, `key`, `namespace`, `tags`
- `status` ∈ `{draft, stable, deprecated}` (default `stable`)
- `generated: {by, at}` (`by` default `"mcp-memory/0.2.0"`)
- `created_at`, `updated_at`
- Optional: `description`, `resource`, `stale_after`, `verified` (event list/map), `sources`, `usage_window`, attested-computation fields

Bundle root index declares `okf_version: "0.2"`.

### 5.2 Target OKF v0.3 (from `specs/01-okf-v03-schema.md` + `templates/scripts/validate_memory.py`)

| Field | Type / constraint |
| :--- | :--- |
| `schema_version` | `"0.3"` (required) |
| `key` | kebab-case `^[a-z0-9]+(?:-[a-z0-9]+)*$`; must equal filename stem |
| `namespace` | closed set: `discovery` \| `architecture` \| `contracts` \| `deployment` \| `sessions` |
| `type` | `story` \| `constraint` \| `adr` \| `contract` \| `runbook` \| `checkpoint` |
| `version` | `int >= 1`; monotonic +1 on update |
| `updated_at` | ISO-8601 UTC |
| `updated_by` | `agent:<id>` \| `human:<username>` |
| `status` | `draft` \| `active` \| `superseded` \| `deprecated` |
| `verified` | **boolean** (agents must not set `true`) |
| `stale_after` | ISO date |
| `supersedes` | optional key or `null` |
| `tags` | list |
| `sources` | list |

Body sections in the example: Context / Decision / Consequences.

### 5.3 Breaking field semantics (v0.2 → v0.3)

| Concern | Current | v0.3 target | Risk |
| :--- | :--- | :--- | :--- |
| Schema marker | none / index `okf_version: "0.2"` | `schema_version: "0.3"` per file | Existing files invalid under template validator |
| Keys | path-like (`user/preferences/theme`, `system/last_memory`) | single kebab-case segment | Path layout + last-memory key break |
| Namespaces | free string; `default` omits folder | fixed five namespaces; always under `memory/<ns>/` | Layout + API default `"default"` incompatible |
| `type` | free / OKF concept types | closed enum of memory record kinds | Parameter rename / mapping needed |
| `status` | `stable` default | `active` (no `stable`) | Existing payloads fail validator |
| `verified` | event list/map | boolean | Signature + serialization break |
| Actor field | `generated.by` | `updated_by` | Rename |
| CAS version | absent | integer `version` in frontmatter | Required for Spec 02 |

---

## 6. Spec hook map (`01`–`04` → existing code)

### 6.1 Spec 01 — OKF v0.3 schema

| Hook point                            | Change                                                                                                            |
| :------------------------------------ | :---------------------------------------------------------------------------------------------------------------- |
| `okf_engine.serialize_okf`            | Emit v0.3 required fields; new defaults; drop/map v0.2-only shapes                                                |
| `okf_engine.parse_okf`                | Expose `version`, `schema_version`, `updated_by`, bool `verified`                                                 |
| `okf_engine.validate_okf_conformance` | Align with template validator rules (or share logic)                                                              |
| `okf_engine.get_memory_file_path`     | Likely always `memory/<namespace>/<key>.md`; kebab-case keys; `sessions` namespace                                |
| `okf_engine.update_directory_index`   | Root `okf_version` / schema marker → `"0.3"`                                                                      |
| `memory_server.memory_store`          | Parameter surface: `concept_type`→`type`, bool `verified`, `updated_by`, drop or gate attested-computation extras |
| `db.store_memory`                     | Pass-through of new metadata; preserve `created_at` policy decision                                               |

**Breaking changes:** yes (frontmatter, status enum, verified type, key/namespace rules, disk paths).  
**Migration policy (resolved):** Full migration to OKF v0.3. Do not maintain dual-schema v0.2 serialization or reading. Update existing assertions in `test_memory.py` to OKF v0.3 conventions (`namespace="discovery"`, kebab-case keys, `status="active"`, boolean `verified`).

**Missing deps:** none beyond existing PyYAML.  
**Fixtures:** `TestOKFEngine` asserts v0.2 strings (`type: Agent Memory`, actor `/` convention, `status: stable`); will need rewrite when Spec 01 lands.

### 6.2 Spec 02 — CAS concurrency

| Hook point | Change |
| :--- | :--- |
| `memory_server.memory_store` | Add `expected_version: int \| None = None`; catch `VersionConflictError` → structured payload |
| `db.store_memory` | Orchestrate lock → version check → atomic markdown write → SQLite txn → unlock |
| `okf_engine.sync_memory_to_disk` | Replace with atomic temp + `flush` + `os.fsync` + `os.replace` |
| New | `VersionConflictError`; lock dir `.mcp_memory/locks/<namespace>_<key>.lock` |
| New tests | `tests/test_cas_concurrency.py` (does not exist) |

**Critical spec vs guardrail conflict:** Spec 02 text says when the file exists, raise if `expected_version is None` **or** mismatch. That would break every current caller (tools omit the param). Guardrails require optional params with **safe defaults**. **Resolved:** Enforce Strict CAS (`expected_version: int = 0`). Update legacy `test_memory.py` calls to pass explicit `expected_version` values.

**Missing deps:** `portalocker` optional; `fcntl` available on this Linux host. Prefer declaring one approach in `requirements.txt` if not relying solely on `fcntl`.

**Fixtures:** `TestMCPTools` / `TestDatabaseLayer` use temp DB + `db.DEFAULT_*` monkeypatch — reusable pattern for concurrency tests; no lock fixtures yet.

### 6.3 Spec 03 — Bidirectional SHA-256 hash sync

| Hook point | Change |
| :--- | :--- |
| `db.init_db` | Add `file_hashes` table **or** hash columns on `memories` |
| New `reconcile_memory_store()` | Scan `memory/**/*.md` (skip `index.md`), hash, upsert/delete, regen index |
| `memory_server` startup | Call reconcile after/instead of bare `init_db()` |
| `memory_retrieve` / `memory_search` | Call reconcile at beginning |
| `db.store_memory` | Persist sha256 + version + `last_synced_at` in same txn as Spec 02 step 4 |
| Logging | Invalid YAML → structured warning on **stderr**, skip file (no crash) |

**Breaking changes:** additive schema; behavior change (disk becomes source of truth for human edits).  
**Missing deps:** none (hashlib stdlib).  
**Fixtures:** none for disk-edit sync; need `tests/test_hash_sync.py`.

### 6.4 Spec 04 — Session-scoped checkpoints

| Hook point | Change |
| :--- | :--- |
| New tools | `memory_checkpoint_set`, `memory_checkpoint_get` |
| `memory_get_last` / `memory_update_last` | Add `session_id: str = "default"`; route to `memory/sessions/<session_id>.md`; add `deprecation_notice` in response |
| Storage | OKF v0.3 file: `namespace: sessions`, `type: checkpoint`, `key: <session_id>` |
| CAS | Per-session file lock + internal version bump (reuse Spec 02 path) |

**Breaking changes:** soft — default `session_id="default"` preserves call shape; **semantic** change from key `system/last_memory` under caller namespace to `memory/sessions/<id>.md`. Existing `system/last_memory` rows become orphans unless a migration/read-fallback is added (not in Spec 04).  
**Fixtures:** `test_last_memory_tools_flow` asserts `system/last_memory` / `type: Checkpoint` — must be updated or dual-pathed.

---

## 7. Guardrail gaps already present in baseline

| Invariant (`00-fork-guardrails.mdc`) | Baseline status |
| :--- | :--- |
| `PRAGMA journal_mode=WAL` | Present |
| `PRAGMA busy_timeout=5000` | **Absent** |
| Atomic markdown writes (`tmp` + `fsync` + `os.replace`) | **Absent** |
| No silent broad `except Exception` | **Violated** in all six MCP tools (returns stringified exception) |
| Structured JSON errors with `error_code` | **Absent** |
| Preserve MCP tool names via optional params | Names stable today; Specs 02/04 extend correctly if defaults are safe |
| One-spec-per-task + pytest under `tests/` | Specs ready; `tests/` empty; suite still root `test_memory.py` |

---

## 8. Existing test fixtures & suite shape

| Item | Detail |
| :--- | :--- |
| Location | `test_memory.py` (root); `tests/` empty |
| Framework | `unittest.TestCase` (pytest-compatible collection) |
| Classes | `TestOKFEngine`, `TestDatabaseLayer`, `TestMCPTools` |
| DB fixture pattern | `tempfile.mkstemp` DB + `mkdtemp` memories dir; assign `db.DEFAULT_DB_PATH` / `DEFAULT_MEMORIES_DIR`; `init_db` |
| MCP fixture pattern | Same DEFAULT monkeypatch; pass `project_root=tempfile.mkdtemp()` into tools |
| Coverage themes | Path traversal guards, OKF v0.2 round-trip, FTS/tags, delete, last-memory flow, project_root override, rejected-key not indexed |
| Planned new files | `tests/test_cas_concurrency.py`, `tests/test_hash_sync.py`, `tests/test_session_scoping.py` |

---

## 9. Implementation order (recommended)

1. **Spec 01** — v0.3 schema + path/namespace rules (foundation for version field).  
2. **Spec 02** — CAS + atomic writes + `busy_timeout` + structured `VERSION_CONFLICT` (resolve `expected_version=None` semantics first).  
3. **Spec 03** — hash table + `reconcile_memory_store` at startup / retrieve / search.  
4. **Spec 04** — session checkpoints + deprecate last-memory tools.

Do not combine specs in one edit pass. Write failing tests under `tests/` before implementation.

---

## 10. Baseline test status (2026-11-17)

**Command:**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest test_memory.py -v
```

**Result:**

| Metric | Value |
| :--- | :--- |
| Collected | 23 items (+ 7 subtests) |
| Passed | **23 passed**, 7 subtests passed |
| Failed | **0** |
| Duration | ~0.4–0.5 s |
| Pytest warning summary (default) | None reported in summary line |
| Notes | With `python -W default -m pytest`, **ResourceWarning: unclosed database** appears for multiple `sqlite3.Connection` objects (connection not closed after context manager). Guardrails target “zero warnings”; closing connections (or using `closing()`) should be part of a later hardening pass, ideally with Spec 02/03 DB work. |
| `tests/` | Empty — no additional suite |

**Verdict:** Baseline is **green** for the existing root suite. Fork specs `01`–`04` are **not** implemented; template validator already encodes v0.3 rules that current `serialize_okf` output would fail.
