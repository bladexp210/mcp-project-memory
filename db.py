"""
Database layer for MCP-Memory server.
Handles SQLite operations, schema initialization, and FTS5 full-text indexing.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
import sqlite3
import sys
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from okf_engine import (
    DEFAULT_NAMESPACE,
    KEY_PATTERN,
    compute_file_sha256,
    compute_payload_sha256,
    get_memory_file_path,
    iter_memory_markdown_files,
    memory_rel_path,
    parse_okf,
    regenerate_memory_indexes,
    remove_memory_from_disk,
    serialize_okf,
    sync_memory_to_disk,
    validate_okf_conformance,
)

SESSIONS_NAMESPACE = "sessions"
CHECKPOINT_TYPE = "checkpoint"

from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()


class VersionConflictError(Exception):
    """Raised when a CAS write's expected_version does not match disk."""

    error_code = "VERSION_CONFLICT"

    def __init__(
        self,
        key: str,
        expected: int,
        actual: int,
        current_record: Optional[str] = None,
    ) -> None:
        self.key = key
        self.expected_version = expected
        self.actual_version = actual
        self.current_record = current_record if current_record is not None else ""
        super().__init__(
            f"VERSION_CONFLICT for key '{key}': expected_version={expected}, "
            f"actual_version={actual}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_code": self.error_code,
            "key": self.key,
            "expected_version": self.expected_version,
            "actual_version": self.actual_version,
            "current_record": self.current_record,
        }


class InvalidSessionIdError(Exception):
    """Raised when a session_id fails kebab-case / path-safety validation."""

    error_code = "INVALID_SESSION_ID"

    def __init__(self, session_id: str, message: Optional[str] = None) -> None:
        self.session_id = session_id
        detail = message or (
            f"Invalid session_id '{session_id}': must be single-segment kebab-case "
            f"matching {KEY_PATTERN.pattern}."
        )
        super().__init__(detail)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_code": self.error_code,
            "session_id": self.session_id,
            "message": str(self),
        }


def validate_session_id(session_id: str) -> str:
    """Validate session_id as a single kebab-case segment (no path traversal)."""
    if session_id is None or not str(session_id).strip():
        raise InvalidSessionIdError(
            session_id if session_id is not None else "",
            "Invalid session_id: must be a non-empty kebab-case identifier.",
        )
    clean = str(session_id).strip()
    if clean.endswith(".md"):
        clean = clean[:-3]
    # Reject separators / traversal before the kebab regex so error_code stays consistent.
    if "/" in clean or "\\" in clean or clean in (".", "..") or ".." in clean:
        raise InvalidSessionIdError(session_id)
    if not KEY_PATTERN.match(clean):
        raise InvalidSessionIdError(session_id)
    return clean


def format_checkpoint_body(
    summary: str,
    active_keys: List[str],
    next_steps: List[str],
) -> str:
    """Render checkpoint markdown sections for summary / active_keys / next_steps."""
    lines: List[str] = ["## summary", "", (summary or "").strip(), ""]
    lines.append("## active_keys")
    lines.append("")
    if active_keys:
        for key in active_keys:
            lines.append(f"- {key}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## next_steps")
    lines.append("")
    if next_steps:
        for step in next_steps:
            lines.append(f"- {step}")
    else:
        lines.append("- (none)")
    return "\n".join(lines)


def set_session_checkpoint(
    session_id: str,
    summary: str,
    active_keys: Optional[List[str]] = None,
    next_steps: Optional[List[str]] = None,
    project_root: Optional[str] = None,
    db_path: Optional[str] = None,
    memories_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create or update ``memory/sessions/<session_id>.md`` with automatic CAS version bump.

    Holds the per-session file lock for the duration of the version read + atomic write
    by delegating to ``store_memory`` (which already serializes writers per key).
    """
    clean_id = validate_session_id(session_id)
    keys = list(active_keys) if active_keys is not None else []
    steps = list(next_steps) if next_steps is not None else []
    body = format_checkpoint_body(summary=summary, active_keys=keys, next_steps=steps)

    # Retry on rare same-session races; distinct session_ids use distinct locks.
    last_conflict: Optional[VersionConflictError] = None
    for _ in range(8):
        existing = retrieve_memory(
            key=clean_id,
            namespace=SESSIONS_NAMESPACE,
            db_path=db_path,
            project_root=project_root,
        )
        expected_version = 0
        if existing is not None:
            expected_version = int(existing["frontmatter"]["version"])
        try:
            return store_memory(
                key=clean_id,
                content=body,
                tags=["checkpoint", "session"],
                namespace=SESSIONS_NAMESPACE,
                concept_type=CHECKPOINT_TYPE,
                title=f"Session Checkpoint ({clean_id})",
                description=(summary or "").strip() or f"Checkpoint for session {clean_id}",
                status="active",
                updated_by="agent:mcp-memory",
                verified=False,
                expected_version=expected_version,
                db_path=db_path,
                memories_dir=memories_dir,
                project_root=project_root,
            )
        except VersionConflictError as exc:
            last_conflict = exc
            continue
    assert last_conflict is not None
    raise last_conflict


def get_session_checkpoint(
    session_id: str,
    project_root: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve a session checkpoint record, or None if missing."""
    clean_id = validate_session_id(session_id)
    return retrieve_memory(
        key=clean_id,
        namespace=SESSIONS_NAMESPACE,
        db_path=db_path,
        project_root=project_root,
    )


def get_project_root() -> Path:
    """Returns the project root directory, or falls back to user home if cwd is root '/' or unwritable."""
    env_root = os.getenv("MCP_MEMORY_PROJECT_ROOT")
    if env_root:
        p = Path(env_root).expanduser().resolve()
        if p != Path("/") and os.access(p, os.W_OK):
            return p

    try:
        cwd = Path.cwd().resolve()
        if cwd != Path("/") and os.access(cwd, os.W_OK):
            return cwd
    except Exception:
        pass

    fallback = Path.home() / ".mcp_memory"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return fallback

def get_default_db_path(override_root: Optional[Union[str, Path]] = None) -> str:
    if override_root is None and "DEFAULT_DB_PATH" in globals() and DEFAULT_DB_PATH != _INITIAL_DB_PATH:
        return DEFAULT_DB_PATH
    raw_db_path = os.getenv("MCP_MEMORY_DB_PATH", ".mcp_memory/memories.db")
    if os.path.isabs(raw_db_path):
        return str(Path(raw_db_path).expanduser().resolve())
    root = Path(override_root).expanduser().resolve() if override_root else get_project_root()
    return str((root / raw_db_path).resolve())

def get_default_memories_dir(override_root: Optional[Union[str, Path]] = None) -> str:
    if override_root is None and "DEFAULT_MEMORIES_DIR" in globals() and DEFAULT_MEMORIES_DIR != _INITIAL_MEM_DIR:
        return DEFAULT_MEMORIES_DIR
    raw_mem_dir = os.getenv("MCP_MEMORY_DIR", "memory")
    if os.path.isabs(raw_mem_dir):
        return str(Path(raw_mem_dir).expanduser().resolve())
    root = Path(override_root).expanduser().resolve() if override_root else get_project_root()
    return str((root / raw_mem_dir).resolve())

_INITIAL_DB_PATH = str((get_project_root() / os.getenv("MCP_MEMORY_DB_PATH", ".mcp_memory/memories.db")).resolve()) if not os.path.isabs(os.getenv("MCP_MEMORY_DB_PATH", "")) else str(Path(os.getenv("MCP_MEMORY_DB_PATH", "")).expanduser().resolve())
_INITIAL_MEM_DIR = str((get_project_root() / os.getenv("MCP_MEMORY_DIR", "memory")).resolve()) if not os.path.isabs(os.getenv("MCP_MEMORY_DIR", "")) else str(Path(os.getenv("MCP_MEMORY_DIR", "")).expanduser().resolve())

DEFAULT_DB_PATH = get_default_db_path()
DEFAULT_MEMORIES_DIR = get_default_memories_dir()


@contextmanager
def get_connection(db_path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with WAL + busy_timeout, closing it on exit."""
    target_path = db_path if db_path is not None else get_default_db_path()
    abs_path = os.path.abspath(os.path.expanduser(target_path))
    parent_dir = os.path.dirname(abs_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    conn = sqlite3.connect(abs_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _locks_dir(
    project_root: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Path:
    if project_root:
        return Path(project_root).expanduser().resolve() / ".mcp_memory" / "locks"
    if db_path is not None:
        return Path(db_path).expanduser().resolve().parent / "locks"
    return Path(get_default_db_path()).resolve().parent / "locks"


def _lock_file_path(
    namespace: str,
    key: str,
    project_root: Optional[str] = None,
    db_path: Optional[str] = None,
) -> Path:
    locks = _locks_dir(project_root=project_root, db_path=db_path)
    locks.mkdir(parents=True, exist_ok=True)
    return locks / f"{namespace}_{key}.lock"


@contextmanager
def _exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite schema including memories table and FTS5 search index."""
    target_path = db_path if db_path is not None else get_default_db_path()
    with get_connection(target_path) as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                key TEXT NOT NULL,
                namespace TEXT NOT NULL DEFAULT 'discovery',
                okf_payload TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (key, namespace)
            );
            """
        )

        # Full-Text Search virtual table
        cursor.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                key UNINDEXED,
                namespace UNINDEXED,
                tags,
                okf_payload
            );
            """
        )

        # Triggers to keep FTS index synced with memories table
        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, key, namespace, tags, okf_payload)
                VALUES (new.rowid, new.key, new.namespace, new.tags, new.okf_payload);
            END;
            """
        )

        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                DELETE FROM memories_fts WHERE rowid = old.rowid;
            END;
            """
        )

        cursor.execute(
            """
            CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                DELETE FROM memories_fts WHERE rowid = old.rowid;
                INSERT INTO memories_fts(rowid, key, namespace, tags, okf_payload)
                VALUES (new.rowid, new.key, new.namespace, new.tags, new.okf_payload);
            END;
            """
        )

        # Spec 03: track on-disk SHA-256 digests for bidirectional HITL sync.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hashes (
                rel_path TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                version INTEGER NOT NULL,
                last_synced_at TEXT NOT NULL
            );
            """
        )

        conn.commit()


def _upsert_file_hash(
    cursor: sqlite3.Cursor,
    rel_path: str,
    sha256: str,
    version: int,
    last_synced_at: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO file_hashes (rel_path, sha256, version, last_synced_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(rel_path) DO UPDATE SET
            sha256 = excluded.sha256,
            version = excluded.version,
            last_synced_at = excluded.last_synced_at
        """,
        (rel_path, sha256, version, last_synced_at),
    )


def _delete_file_hash(cursor: sqlite3.Cursor, rel_path: str) -> None:
    cursor.execute("DELETE FROM file_hashes WHERE rel_path = ?", (rel_path,))


def _emit_reconcile_warning(rel_path: str, message: str, errors: Optional[List[str]] = None) -> None:
    payload = {
        "level": "warning",
        "error_code": "RECONCILE_SKIP",
        "rel_path": rel_path,
        "message": message,
        "errors": errors or [],
    }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)


def store_memory(
    key: str,
    content: Union[str, Dict[str, Any]],
    tags: Optional[List[str]] = None,
    namespace: str = DEFAULT_NAMESPACE,
    concept_type: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    resource: Optional[str] = None,
    status: Optional[str] = None,
    stale_after: Optional[str] = None,
    sources: Optional[List[Any]] = None,
    usage_window: Optional[Dict[str, str]] = None,
    verified: Optional[bool] = None,
    generated_by: Optional[str] = None,
    updated_by: Optional[str] = None,
    version: Optional[int] = None,
    expected_version: int = 0,
    supersedes: Optional[str] = None,
    runtime: Optional[str] = None,
    parameters: Optional[List[Dict[str, Any]]] = None,
    executor: Optional[Dict[str, Any]] = None,
    attester: Optional[Dict[str, Any]] = None,
    computation: Optional[str] = None,
    db_path: Optional[str] = None,
    memories_dir: Optional[str] = None,
    project_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Store or update a memory record with strict CAS (expected_version)."""
    target_path = db_path if db_path is not None else get_default_db_path(override_root=project_root)
    target_dir = memories_dir if memories_dir is not None else get_default_memories_dir(override_root=project_root)

    # Resolve the destination before touching SQLite so a key or namespace that
    # cannot be written raises here rather than after the row is committed.
    file_path = get_memory_file_path(key=key, namespace=namespace, base_dir=target_dir)

    init_db(target_path)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tags_list = tags if tags is not None else []
    tags_json = json.dumps(tags_list)
    lock_path = _lock_file_path(
        namespace=namespace,
        key=key,
        project_root=project_root,
        db_path=target_path,
    )

    with _exclusive_file_lock(lock_path):
        current_record: Optional[str] = None
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as existing_file:
                current_record = existing_file.read()
            actual_version = int(parse_okf(current_record)["frontmatter"]["version"])
            if expected_version != actual_version:
                raise VersionConflictError(
                    key=key,
                    expected=expected_version,
                    actual=actual_version,
                    current_record=current_record,
                )
            next_version = actual_version + 1
        else:
            if expected_version != 0:
                raise VersionConflictError(
                    key=key,
                    expected=expected_version,
                    actual=0,
                    current_record="",
                )
            next_version = 1

        # Caller-supplied `version` cannot bypass CAS; disk/CAS decides the value.
        _ = version
        cas_version = next_version

        with get_connection(target_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT created_at FROM memories WHERE key = ? AND namespace = ?",
                (key, namespace),
            )
            row = cursor.fetchone()
            created_at = row["created_at"] if row else now_iso

            okf_payload = serialize_okf(
                key=key,
                content=content,
                tags=tags_list,
                namespace=namespace,
                concept_type=concept_type,
                title=title,
                description=description,
                resource=resource,
                status=status,
                stale_after=stale_after,
                sources=sources,
                usage_window=usage_window,
                verified=verified,
                generated_by=generated_by,
                updated_by=updated_by,
                version=cas_version,
                supersedes=supersedes,
                runtime=runtime,
                parameters=parameters,
                executor=executor,
                attester=attester,
                computation=computation,
                created_at=created_at,
                updated_at=now_iso,
            )

            # Atomic markdown write first, then SQLite (Spec 02 order).
            synced_path = sync_memory_to_disk(
                key=key,
                namespace=namespace,
                okf_payload=okf_payload,
                base_dir=target_dir,
            )

            cursor.execute(
                """
                INSERT INTO memories (key, namespace, okf_payload, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key, namespace) DO UPDATE SET
                    okf_payload = excluded.okf_payload,
                    tags = excluded.tags,
                    updated_at = excluded.updated_at
                """,
                (key, namespace, okf_payload, tags_json, created_at, now_iso),
            )

            # Keep file_hashes in the same transaction as the memories upsert.
            rel_path = memory_rel_path(namespace, key)
            digest = compute_payload_sha256(okf_payload)
            _upsert_file_hash(
                cursor,
                rel_path=rel_path,
                sha256=digest,
                version=cas_version,
                last_synced_at=now_iso,
            )

    return {
        "key": key,
        "namespace": namespace,
        "tags": tags_list,
        "created_at": created_at,
        "updated_at": now_iso,
        "okf_payload": okf_payload,
        "file_path": synced_path,
    }


def retrieve_memory(
    key: str,
    namespace: str = DEFAULT_NAMESPACE,
    db_path: Optional[str] = None,
    project_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve a memory record by key and namespace."""
    target_path = db_path if db_path is not None else get_default_db_path(override_root=project_root)
    init_db(target_path)
    with get_connection(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT key, namespace, okf_payload, tags, created_at, updated_at FROM memories WHERE key = ? AND namespace = ?",
            (key, namespace),
        )
        row = cursor.fetchone()
        if not row:
            return None

        parsed = parse_okf(row["okf_payload"])
        parsed["created_at"] = row["created_at"]
        parsed["updated_at"] = row["updated_at"]
        return parsed


def search_memories(
    query: Optional[str] = None,
    tags: Optional[List[str]] = None,
    namespace: Optional[str] = None,
    limit: int = 10,
    db_path: Optional[str] = None,
    project_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Search memories using SQLite FTS5 or SQL filtering.
    Filters by query keywords, tags list, and optional namespace.
    """
    target_path = db_path if db_path is not None else get_default_db_path(override_root=project_root)
    init_db(target_path)
    results = []

    with get_connection(target_path) as conn:
        cursor = conn.cursor()
        
        sql_conditions = []
        params: List[Any] = []

        if namespace:
            sql_conditions.append("m.namespace = ?")
            params.append(namespace)

        if query and query.strip():
            # Clean query term for FTS matching
            escaped_terms = [term.replace('"', '""') for term in query.strip().split()]
            fts_query = " ".join(f'"{t}"*' for t in escaped_terms)
            sql = """
                SELECT m.key, m.namespace, m.okf_payload, m.tags, m.created_at, m.updated_at
                FROM memories m
                JOIN memories_fts fts ON m.rowid = fts.rowid
                WHERE memories_fts MATCH ?
            """
            params.insert(0, fts_query)

            if sql_conditions:
                sql += " AND " + " AND ".join(sql_conditions)

            sql += " ORDER BY rank LIMIT ?"
            params.append(limit * 2) # Fetch extra candidate records for tag filtering if needed
        else:
            sql = "SELECT key, namespace, okf_payload, tags, created_at, updated_at FROM memories m"
            if sql_conditions:
                sql += " WHERE " + " AND ".join(sql_conditions)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit * 2)

        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Fallback to LIKE search if FTS query syntax error occurs
            like_sql = "SELECT key, namespace, okf_payload, tags, created_at, updated_at FROM memories m WHERE 1=1"
            like_params: List[Any] = []
            if query:
                like_sql += " AND (m.key LIKE ? OR m.okf_payload LIKE ?)"
                like_params.extend([f"%{query}%", f"%{query}%"])
            if namespace:
                like_sql += " AND m.namespace = ?"
                like_params.append(namespace)
            like_sql += " ORDER BY updated_at DESC LIMIT ?"
            like_params.append(limit * 2)
            cursor.execute(like_sql, like_params)
            rows = cursor.fetchall()

        for row in rows:
            parsed = parse_okf(row["okf_payload"])
            record_tags = parsed.get("tags", [])
            
            # Check tag filters
            if tags:
                if not any(tag in record_tags for tag in tags):
                    continue

            results.append({
                "key": row["key"],
                "namespace": row["namespace"],
                "tags": record_tags,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "okf_payload": row["okf_payload"],
                "parsed": parsed,
            })

            if len(results) >= limit:
                break

    return results


def delete_memory(
    key: str,
    namespace: str = DEFAULT_NAMESPACE,
    db_path: Optional[str] = None,
    memories_dir: Optional[str] = None,
    project_root: Optional[str] = None,
) -> bool:
    """Delete a memory record by key and namespace from SQLite and disk."""
    target_path = db_path if db_path is not None else get_default_db_path(override_root=project_root)
    target_dir = memories_dir if memories_dir is not None else get_default_memories_dir(override_root=project_root)

    # Same check as store_memory: a key we cannot resolve must not drop the row.
    get_memory_file_path(key=key, namespace=namespace, base_dir=target_dir)

    init_db(target_path)
    rel_path = memory_rel_path(namespace, key)
    with get_connection(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM memories WHERE key = ? AND namespace = ?",
            (key, namespace),
        )
        deleted = cursor.rowcount > 0
        if deleted:
            _delete_file_hash(cursor, rel_path)

    if deleted:
        remove_memory_from_disk(key=key, namespace=namespace, base_dir=target_dir)

    return deleted


def _key_namespace_from_rel_path(rel_path: str) -> Optional[Tuple[str, str]]:
    """Parse ``namespace/key.md`` into ``(namespace, key)``; return None if invalid."""
    normalized = rel_path.replace("\\", "/").strip("/")
    parts = [p for p in normalized.split("/") if p]
    if len(parts) != 2 or not parts[1].endswith(".md"):
        return None
    namespace = parts[0]
    key = parts[1][:-3]
    return namespace, key


def reconcile_memory_store(
    project_root: Optional[str] = None,
    db_path: Optional[str] = None,
    memories_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Synchronize SQLite (+ FTS5) with on-disk ``memory/**/*.md`` via SHA-256 digests.

    Disk is the canonical HITL source: added/modified files are upserted without
    CAS checks. Invalid YAML / non-conforming OKF files are skipped with a
    structured stderr warning. ``memory/index.md`` is regenerated only when at
    least one add, modify, or delete occurred.
    """
    target_path = (
        db_path if db_path is not None else get_default_db_path(override_root=project_root)
    )
    target_dir = (
        memories_dir
        if memories_dir is not None
        else get_default_memories_dir(override_root=project_root)
    )

    init_db(target_path)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    disk_files = list(iter_memory_markdown_files(target_dir))
    disk_by_rel = {rel: abs_path for rel, abs_path in disk_files}
    disk_hashes: Dict[str, str] = {}
    for rel, abs_path in disk_files:
        try:
            disk_hashes[rel] = compute_file_sha256(abs_path)
        except OSError as exc:
            _emit_reconcile_warning(rel, f"Failed to read file for hashing: {exc}")

    added = 0
    modified = 0
    deleted = 0
    skipped = 0

    with get_connection(target_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT rel_path, sha256 FROM file_hashes")
        db_hashes = {row["rel_path"]: row["sha256"] for row in cursor.fetchall()}

        # Deleted on disk: present in SQLite hashes, missing from disk.
        for rel_path in list(db_hashes.keys()):
            if rel_path in disk_hashes:
                continue
            parsed_ids = _key_namespace_from_rel_path(rel_path)
            if parsed_ids is not None:
                namespace, key = parsed_ids
                cursor.execute(
                    "DELETE FROM memories WHERE key = ? AND namespace = ?",
                    (key, namespace),
                )
            _delete_file_hash(cursor, rel_path)
            deleted += 1

        # Added or modified on disk.
        for rel_path, digest in disk_hashes.items():
            db_digest = db_hashes.get(rel_path)
            if db_digest is not None and db_digest == digest:
                continue

            abs_path = disk_by_rel[rel_path]
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    raw_payload = f.read()
            except OSError as exc:
                _emit_reconcile_warning(rel_path, f"Failed to read file: {exc}")
                skipped += 1
                continue

            validation = validate_okf_conformance(raw_payload, file_path=abs_path)
            if not validation["valid"]:
                _emit_reconcile_warning(
                    rel_path,
                    "Invalid YAML or missing required OKF v0.3 fields; skipping",
                    errors=list(validation["errors"]),
                )
                skipped += 1
                continue

            try:
                parsed = parse_okf(raw_payload)
            except ValueError as exc:
                _emit_reconcile_warning(
                    rel_path,
                    f"Failed to parse OKF document; skipping: {exc}",
                )
                skipped += 1
                continue

            fm = parsed["frontmatter"]
            key = str(fm["key"])
            namespace = str(fm["namespace"])
            version = int(fm["version"])
            tags_list = list(parsed.get("tags") or [])
            tags_json = json.dumps(tags_list)
            updated_at = str(fm.get("updated_at") or now_iso)

            cursor.execute(
                "SELECT created_at FROM memories WHERE key = ? AND namespace = ?",
                (key, namespace),
            )
            row = cursor.fetchone()
            created_at = row["created_at"] if row else now_iso

            cursor.execute(
                """
                INSERT INTO memories (key, namespace, okf_payload, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key, namespace) DO UPDATE SET
                    okf_payload = excluded.okf_payload,
                    tags = excluded.tags,
                    updated_at = excluded.updated_at
                """,
                (key, namespace, raw_payload, tags_json, created_at, updated_at),
            )
            _upsert_file_hash(
                cursor,
                rel_path=rel_path,
                sha256=digest,
                version=version,
                last_synced_at=now_iso,
            )

            if db_digest is None:
                added += 1
            else:
                modified += 1

    changed = (added + modified + deleted) > 0
    if changed:
        regenerate_memory_indexes(target_dir)

    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "skipped": skipped,
        "changed": changed,
    }
