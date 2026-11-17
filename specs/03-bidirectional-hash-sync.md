# Spec 03: Bidirectional SHA-256 Hash Sync

## Objective
Keep `.mcp_memory/memories.db` synchronized with `memory/**/*.md` when humans edit, add, or delete Markdown files directly or via Git operations (`git pull`, `git checkout`, `git revert`).

## SQLite Schema Addition
Add a `file_hashes` table (or `content_hash TEXT NOT NULL` column on the primary `memories` table):
- `rel_path TEXT PRIMARY KEY` (e.g., `"architecture/adr-004-sqlite-wal-sync.md"`)
- `sha256 TEXT NOT NULL`
- `version INTEGER NOT NULL`
- `last_synced_at TEXT NOT NULL`

## Reconciliation Algorithm (`reconcile_memory_store()`)
Execute `reconcile_memory_store()` at server startup and at the beginning of `memory_search` and `memory_retrieve`:
1. Scan `memory/**/*.md` (excluding `memory/index.md`) and compute the SHA-256 hex digest of each file's raw UTF-8 bytes.
2. Load all `(rel_path, sha256)` rows from SQLite.
3. **Deleted on Disk:** For any `rel_path` in SQLite missing from disk, delete its row from `memories` and `FTS5` index.
4. **Added or Modified on Disk:** For any file missing from SQLite or whose disk `sha256 != db_sha256`:
   - Parse YAML frontmatter and Markdown body.
   - If frontmatter is invalid YAML or missing required OKF v0.3 fields, log a structured warning to `stderr` and skip indexing that file without crashing the server.
   - Upsert the parsed record and new `sha256` into SQLite.
5. Regenerate `memory/index.md` only if at least one file was added, modified, or deleted during reconciliation.

## Human Authority Rule
Manual Git edits ingested during reconciliation bypass `expected_version` CAS checks because disk state is the canonical HITL audit source.

## Acceptance Criteria (`tests/test_hash_sync.py`)
- Editing a file's body on disk outside MCP causes the next `memory_search` call to return updated content within one call.
- Deleting a `.md` file from disk removes it from `memory_search` results on the next query.
- Unchanged files incur zero SQLite write transactions during `reconcile_memory_store()`.