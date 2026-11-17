"""
Failing-first tests for Spec 03: Bidirectional SHA-256 Hash Sync.

Covers disk edits/adds/deletes reflected on next retrieve/search,
malformed YAML warnings, and no-op reconcile (zero SQLite writes / no index rewrite).
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest import mock

import db
from memory_server import memory_retrieve, memory_search
from okf_engine import (
    serialize_okf,
    validate_okf_conformance,
)


def _stale_after(days: int = 90) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def _valid_okf_payload(
    *,
    key: str,
    namespace: str = "discovery",
    concept_type: str = "story",
    content: str = "Original body content.",
    version: int = 1,
    updated_by: str = "human:editor",
    tags: Optional[List[str]] = None,
    **overrides: Any,
) -> str:
    kwargs: Dict[str, Any] = {
        "key": key,
        "content": content,
        "namespace": namespace,
        "concept_type": concept_type,
        "status": "active",
        "stale_after": _stale_after(),
        "tags": tags if tags is not None else ["hash-sync"],
        "sources": [],
        "updated_by": updated_by,
        "verified": False,
        "version": version,
    }
    kwargs.update(overrides)
    return serialize_okf(**kwargs)


class _HashSyncFixture(unittest.TestCase):
    def setUp(self):
        self.project_root = tempfile.mkdtemp()
        self.temp_memories_dir = os.path.join(self.project_root, "memory")
        self.temp_db_path = os.path.join(
            self.project_root, ".mcp_memory", "memories.db"
        )
        os.makedirs(self.temp_memories_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.temp_db_path), exist_ok=True)
        db.DEFAULT_DB_PATH = self.temp_db_path
        db.DEFAULT_MEMORIES_DIR = self.temp_memories_dir
        db.init_db(self.temp_db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.project_root, ignore_errors=True)

    def _store(
        self,
        key: str,
        content: str,
        *,
        namespace: str = "discovery",
        concept_type: str = "story",
        expected_version: int = 0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return db.store_memory(
            key=key,
            content=content,
            namespace=namespace,
            concept_type=concept_type,
            expected_version=expected_version,
            db_path=self.temp_db_path,
            memories_dir=self.temp_memories_dir,
            project_root=self.project_root,
            **kwargs,
        )

    def _write_disk_file(
        self,
        rel_path: str,
        payload: str,
    ) -> str:
        abs_path = os.path.join(self.temp_memories_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(payload)
        return abs_path


class TestDiskEditReflectedOnRetrieveAndSearch(_HashSyncFixture):
    def test_editing_md_body_or_frontmatter_on_disk_updates_retrieve_and_search(self):
        created = self._store(
            key="adr-hash-edit",
            content="Original body before HITL edit.",
            namespace="architecture",
            concept_type="adr",
        )
        file_path = created["file_path"]
        self.assertTrue(os.path.exists(file_path))

        edited = _valid_okf_payload(
            key="adr-hash-edit",
            namespace="architecture",
            concept_type="adr",
            content="HITL-edited body with unique-token-xyz.",
            version=7,
            updated_by="human:mohamed",
            tags=["edited-on-disk"],
        )
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(edited)

        retrieved = memory_retrieve(
            key="adr-hash-edit",
            project_root=self.project_root,
            namespace="architecture",
        )
        self.assertIn("HITL-edited body with unique-token-xyz.", retrieved)
        self.assertIn("version: 7", retrieved)
        self.assertIn("updated_by: human:mohamed", retrieved)
        self.assertNotIn("Original body before HITL edit.", retrieved)

        search = memory_search(
            project_root=self.project_root,
            query="unique-token-xyz",
            namespace="architecture",
        )
        self.assertIn("unique-token-xyz", search)
        self.assertIn("version: 7", search)
        self.assertIn("adr-hash-edit", search)

        # SQLite should also reflect the disk version after reconcile.
        record = db.retrieve_memory(
            key="adr-hash-edit",
            namespace="architecture",
            db_path=self.temp_db_path,
            project_root=self.project_root,
        )
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["frontmatter"]["version"], 7)
        self.assertIn("unique-token-xyz", record["body"])


class TestDiskAddDiscoverable(_HashSyncFixture):
    def test_new_okf_file_on_disk_is_discoverable_via_search_and_retrieve(self):
        payload = _valid_okf_payload(
            key="story-disk-add",
            namespace="discovery",
            content="Brand new disk-only memory discover-me-token.",
            version=1,
            tags=["disk-add"],
        )
        rel = "discovery/story-disk-add.md"
        self._write_disk_file(rel, payload)
        validation = validate_okf_conformance(
            payload,
            file_path=os.path.join(self.temp_memories_dir, rel),
        )
        self.assertTrue(validation["valid"], validation["errors"])

        search = memory_search(
            project_root=self.project_root,
            query="discover-me-token",
            namespace="discovery",
        )
        self.assertIn("story-disk-add", search)
        self.assertIn("discover-me-token", search)

        retrieved = memory_retrieve(
            key="story-disk-add",
            project_root=self.project_root,
            namespace="discovery",
        )
        self.assertIn("discover-me-token", retrieved)
        self.assertIn("version: 1", retrieved)


class TestDiskDeletePurgesSqliteAndFts(_HashSyncFixture):
    def test_deleting_md_on_disk_removes_from_sqlite_and_search(self):
        created = self._store(
            key="story-disk-delete",
            content="Soon deleted from disk purge-token.",
            namespace="discovery",
            concept_type="story",
        )
        file_path = created["file_path"]
        self.assertTrue(os.path.exists(file_path))

        # Confirm searchable before delete.
        before = memory_search(
            project_root=self.project_root,
            query="purge-token",
            namespace="discovery",
        )
        self.assertIn("story-disk-delete", before)

        os.remove(file_path)
        self.assertFalse(os.path.exists(file_path))

        after = memory_search(
            project_root=self.project_root,
            query="purge-token",
            namespace="discovery",
        )
        self.assertEqual(after, "No matching memories found.")

        retrieved = memory_retrieve(
            key="story-disk-delete",
            project_root=self.project_root,
            namespace="discovery",
        )
        self.assertIn("not found", retrieved.lower())

        record = db.retrieve_memory(
            key="story-disk-delete",
            namespace="discovery",
            db_path=self.temp_db_path,
            project_root=self.project_root,
        )
        self.assertIsNone(record)


class TestMalformedYamlSkipped(_HashSyncFixture):
    def test_malformed_yaml_logs_stderr_warning_and_does_not_crash_reconcile(self):
        # Seed one valid file so reconcile has other work to do.
        self._store(
            key="story-valid-neighbor",
            content="Valid neighbor content.",
            namespace="discovery",
        )

        # Intentionally invalid YAML frontmatter (unclosed quote / bad mapping).
        malformed = (
            "---\n"
            "schema_version: '0.3\n"
            "key: [broken\n"
            "namespace: discovery\n"
            "---\n\n"
            "Body that should be skipped.\n"
        )
        # Use a .md path that would otherwise look like a memory file.
        bad_path = self._write_disk_file("discovery/broken-frontmatter.md", malformed)
        self.assertTrue(os.path.exists(bad_path))

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            # Must not raise.
            result = db.reconcile_memory_store(
                project_root=self.project_root,
                db_path=self.temp_db_path,
                memories_dir=self.temp_memories_dir,
            )

        stderr_text = stderr_buf.getvalue()
        self.assertTrue(
            stderr_text.strip(),
            "expected a structured warning on stderr for malformed YAML",
        )
        # Structured warning should identify the path and that it was skipped.
        self.assertIn("broken-frontmatter.md", stderr_text)
        lower = stderr_text.lower()
        self.assertTrue(
            "warn" in lower or "skip" in lower or "invalid" in lower or "error" in lower,
            f"stderr warning not structured enough: {stderr_text!r}",
        )

        # Valid neighbor still present; malformed file not indexed.
        neighbor = db.retrieve_memory(
            key="story-valid-neighbor",
            namespace="discovery",
            db_path=self.temp_db_path,
            project_root=self.project_root,
        )
        self.assertIsNotNone(neighbor)

        # Reconcile should report without crashing; result may be a summary dict/None.
        _ = result

        search = memory_search(
            project_root=self.project_root,
            query="should be skipped",
            namespace="discovery",
        )
        self.assertEqual(search, "No matching memories found.")


class TestNoOpReconcile(_HashSyncFixture):
    def test_unchanged_files_incur_zero_sqlite_writes_and_do_not_rewrite_index(self):
        created = self._store(
            key="story-noop-sync",
            content="Stable content for no-op reconcile.",
            namespace="discovery",
        )
        # First reconcile after store should sync hashes if needed, then settle.
        db.reconcile_memory_store(
            project_root=self.project_root,
            db_path=self.temp_db_path,
            memories_dir=self.temp_memories_dir,
        )

        index_path = os.path.join(self.temp_memories_dir, "index.md")
        # Ensure index exists after store/reconcile.
        self.assertTrue(
            os.path.exists(index_path) or os.path.exists(
                os.path.join(self.temp_memories_dir, "discovery", "index.md")
            ),
            "expected an index.md under the memory bundle",
        )
        # Prefer bundle-root index when present; else namespace index.
        tracked_index = index_path
        if not os.path.exists(tracked_index):
            tracked_index = os.path.join(
                self.temp_memories_dir, "discovery", "index.md"
            )
        with open(tracked_index, "rb") as f:
            index_before = f.read()
        index_mtime_before = os.path.getmtime(tracked_index)

        write_ops: List[str] = []
        real_get_connection = db.get_connection

        def tracking_connection(*args, **kwargs):
            cm = real_get_connection(*args, **kwargs)

            class _TrackingCM:
                def __enter__(self_inner):
                    conn = cm.__enter__()
                    before_changes = conn.total_changes

                    def tracer(sql: str) -> None:
                        sql_stripped = " ".join(sql.split()).upper()
                        if sql_stripped.startswith(
                            ("INSERT", "UPDATE", "DELETE", "REPLACE")
                        ):
                            write_ops.append(sql_stripped.split()[0])

                    conn.set_trace_callback(tracer)
                    self_inner._conn = conn
                    self_inner._before_changes = before_changes
                    return conn

                def __exit__(self_inner, exc_type, exc, tb):
                    conn = self_inner._conn
                    after_changes = conn.total_changes
                    if after_changes != self_inner._before_changes and not write_ops:
                        write_ops.append(
                            f"TOTAL_CHANGES_DELTA:{after_changes - self_inner._before_changes}"
                        )
                    conn.set_trace_callback(None)
                    return cm.__exit__(exc_type, exc, tb)

            return _TrackingCM()

        with mock.patch.object(db, "get_connection", side_effect=tracking_connection):
            db.reconcile_memory_store(
                project_root=self.project_root,
                db_path=self.temp_db_path,
                memories_dir=self.temp_memories_dir,
            )

        self.assertEqual(
            write_ops,
            [],
            f"expected zero SQLite write transactions/DML, got: {write_ops}",
        )

        with open(tracked_index, "rb") as f:
            index_after = f.read()
        index_mtime_after = os.path.getmtime(tracked_index)
        self.assertEqual(index_before, index_after)
        self.assertEqual(index_mtime_before, index_mtime_after)

        # Sanity: file still present and hashable.
        with open(created["file_path"], "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()
