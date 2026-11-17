"""
Failing-first tests for Spec 02: Optimistic Concurrency Control (CAS).

Covers strict expected_version semantics, VersionConflictError payloads,
two-thread race outcomes, and multi-thread markdown integrity under stress.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

import db
from db import VersionConflictError
from memory_server import memory_store
from okf_engine import parse_okf, validate_okf_conformance


class _CasFixture(unittest.TestCase):
    def setUp(self):
        self.temp_db_fd, self.temp_db_path = tempfile.mkstemp(suffix=".db")
        self.temp_memories_dir = tempfile.mkdtemp()
        self.project_root = tempfile.mkdtemp()
        db.DEFAULT_DB_PATH = self.temp_db_path
        db.DEFAULT_MEMORIES_DIR = self.temp_memories_dir
        db.init_db(self.temp_db_path)

    def tearDown(self):
        os.close(self.temp_db_fd)
        if os.path.exists(self.temp_db_path):
            os.remove(self.temp_db_path)
        import shutil

        shutil.rmtree(self.temp_memories_dir, ignore_errors=True)
        shutil.rmtree(self.project_root, ignore_errors=True)


class TestCasStrictVersioning(_CasFixture):
    def test_create_with_expected_version_zero_succeeds_at_version_one(self):
        record = db.store_memory(
            key="story-cas-create",
            content="Initial create under CAS",
            concept_type="story",
            expected_version=0,
            db_path=self.temp_db_path,
            memories_dir=self.temp_memories_dir,
            project_root=self.project_root,
        )
        parsed = parse_okf(record["okf_payload"])
        self.assertEqual(parsed["frontmatter"]["version"], 1)
        self.assertTrue(os.path.exists(record["file_path"]))

    def test_create_with_nonzero_expected_version_raises_conflict(self):
        with self.assertRaises(VersionConflictError) as ctx:
            db.store_memory(
                key="story-cas-missing",
                content="Should not create",
                concept_type="story",
                expected_version=1,
                db_path=self.temp_db_path,
                memories_dir=self.temp_memories_dir,
                project_root=self.project_root,
            )
        err = ctx.exception
        self.assertEqual(err.error_code, "VERSION_CONFLICT")
        self.assertEqual(err.expected_version, 1)
        self.assertEqual(err.actual_version, 0)
        self.assertEqual(err.key, "story-cas-missing")

    def test_update_with_matching_expected_version_increments(self):
        first = db.store_memory(
            key="story-cas-update",
            content="v1 body",
            concept_type="story",
            expected_version=0,
            db_path=self.temp_db_path,
            memories_dir=self.temp_memories_dir,
            project_root=self.project_root,
        )
        self.assertEqual(parse_okf(first["okf_payload"])["frontmatter"]["version"], 1)

        second = db.store_memory(
            key="story-cas-update",
            content="v2 body",
            concept_type="story",
            expected_version=1,
            db_path=self.temp_db_path,
            memories_dir=self.temp_memories_dir,
            project_root=self.project_root,
        )
        self.assertEqual(parse_okf(second["okf_payload"])["frontmatter"]["version"], 2)
        self.assertIn("v2 body", second["okf_payload"])

    def test_update_with_mismatched_expected_version_raises_structured_conflict(self):
        created = db.store_memory(
            key="story-cas-mismatch",
            content="canonical body",
            concept_type="story",
            expected_version=0,
            db_path=self.temp_db_path,
            memories_dir=self.temp_memories_dir,
            project_root=self.project_root,
        )
        current_path = created["file_path"]
        with open(current_path, "r", encoding="utf-8") as f:
            disk_record = f.read()

        with self.assertRaises(VersionConflictError) as ctx:
            db.store_memory(
                key="story-cas-mismatch",
                content="stale writer body",
                concept_type="story",
                expected_version=99,
                db_path=self.temp_db_path,
                memories_dir=self.temp_memories_dir,
                project_root=self.project_root,
            )
        err = ctx.exception
        self.assertEqual(err.error_code, "VERSION_CONFLICT")
        self.assertEqual(err.expected_version, 99)
        self.assertEqual(err.actual_version, 1)
        self.assertEqual(err.key, "story-cas-mismatch")
        self.assertEqual(err.current_record, disk_record)

    def test_create_existing_key_with_expected_version_zero_conflicts(self):
        db.store_memory(
            key="story-cas-exists",
            content="already here",
            concept_type="story",
            expected_version=0,
            db_path=self.temp_db_path,
            memories_dir=self.temp_memories_dir,
            project_root=self.project_root,
        )
        with self.assertRaises(VersionConflictError) as ctx:
            db.store_memory(
                key="story-cas-exists",
                content="overwrite attempt",
                concept_type="story",
                expected_version=0,
                db_path=self.temp_db_path,
                memories_dir=self.temp_memories_dir,
                project_root=self.project_root,
            )
        err = ctx.exception
        self.assertEqual(err.error_code, "VERSION_CONFLICT")
        self.assertEqual(err.expected_version, 0)
        self.assertEqual(err.actual_version, 1)


class TestCasConcurrentMemoryStore(_CasFixture):
    def setUp(self):
        super().setUp()
        # Route MCP tools through the same temp project root used for locks/DB/files.
        self.mcp_root = tempfile.mkdtemp()
        mem_dir = os.path.join(self.mcp_root, "memory")
        db_path = os.path.join(self.mcp_root, ".mcp_memory", "memories.db")
        os.makedirs(mem_dir, exist_ok=True)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        db.DEFAULT_DB_PATH = db_path
        db.DEFAULT_MEMORIES_DIR = mem_dir
        db.init_db(db_path)
        self.mcp_db_path = db_path
        self.mcp_mem_dir = mem_dir

    def tearDown(self):
        import shutil

        shutil.rmtree(self.mcp_root, ignore_errors=True)
        super().tearDown()

    def test_two_threads_one_success_one_version_conflict(self):
        create = memory_store(
            key="story-race-key",
            content="seed v1",
            concept_type="story",
            expected_version=0,
            project_root=self.mcp_root,
        )
        self.assertIn("Successfully stored memory", create)
        self.assertIn("version: 1", create)

        barrier = threading.Barrier(2)
        results: List[Optional[str]] = [None, None]

        def worker(idx: int) -> None:
            barrier.wait(timeout=5)
            results[idx] = memory_store(
                key="story-race-key",
                content=f"racer-{idx}",
                concept_type="story",
                expected_version=1,
                project_root=self.mcp_root,
            )

        threads = [
            threading.Thread(target=worker, args=(0,)),
            threading.Thread(target=worker, args=(1,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])

        successes = [r for r in results if r and "Successfully stored memory" in r]
        conflicts = []
        for r in results:
            if r and "Successfully stored memory" not in r:
                payload = json.loads(r)
                conflicts.append(payload)

        self.assertEqual(len(successes), 1, results)
        self.assertEqual(len(conflicts), 1, results)
        self.assertIn("version: 2", successes[0])
        conflict = conflicts[0]
        self.assertEqual(conflict["error_code"], "VERSION_CONFLICT")
        self.assertEqual(conflict["expected_version"], 1)
        self.assertEqual(conflict["actual_version"], 2)
        self.assertIn("current_record", conflict)
        self.assertIn("version: 2", conflict["current_record"])

    def test_ten_thread_stress_produces_no_truncated_markdown(self):
        create = memory_store(
            key="story-stress-key",
            content="seed",
            concept_type="story",
            expected_version=0,
            project_root=self.mcp_root,
        )
        self.assertIn("Successfully stored memory", create)

        file_path = os.path.join(
            self.mcp_mem_dir, "discovery", "story-stress-key.md"
        )
        lock = threading.Lock()
        outcomes: List[Tuple[str, Optional[str]]] = []

        def racing_writer(worker_id: int) -> None:
            # Retry with CAS until this worker lands one successful write.
            attempts = 0
            while attempts < 50:
                attempts += 1
                with open(file_path, "r", encoding="utf-8") as f:
                    current = f.read()
                actual = int(parse_okf(current)["frontmatter"]["version"])
                result = memory_store(
                    key="story-stress-key",
                    content=f"worker-{worker_id}-attempt-{attempts}-" + ("x" * 200),
                    concept_type="story",
                    expected_version=actual,
                    project_root=self.mcp_root,
                )
                if "Successfully stored memory" in result:
                    with lock:
                        outcomes.append(("ok", result))
                    return
                payload = json.loads(result)
                self.assertEqual(payload["error_code"], "VERSION_CONFLICT")
            with lock:
                outcomes.append(("fail", None))

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(racing_writer, i) for i in range(10)]
            for fut in as_completed(futures):
                fut.result(timeout=30)

        self.assertEqual(len(outcomes), 10)
        self.assertTrue(all(kind == "ok" for kind, _ in outcomes), outcomes)

        # Unique keys written concurrently must also remain intact.
        def unique_writer(i: int) -> str:
            return memory_store(
                key=f"story-unique-{i}",
                content=f"unique body {i}\n" + ("y" * 500),
                concept_type="story",
                expected_version=0,
                project_root=self.mcp_root,
            )

        with ThreadPoolExecutor(max_workers=10) as pool:
            unique_results = list(pool.map(unique_writer, range(10)))
        self.assertTrue(all("Successfully stored memory" in r for r in unique_results))

        md_files = []
        for root, _dirs, files in os.walk(self.mcp_mem_dir):
            for name in files:
                if name.endswith(".md") and name not in ("index.md", "log.md"):
                    md_files.append(os.path.join(root, name))

        self.assertGreaterEqual(len(md_files), 11)
        for path in md_files:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            self.assertTrue(raw.startswith("---\n"), f"truncated/corrupt frontmatter open: {path}")
            self.assertGreaterEqual(raw.count("\n---\n"), 1, f"missing closing --- in {path}")
            parsed = parse_okf(raw)
            validation = validate_okf_conformance(raw, file_path=path)
            self.assertTrue(
                validation["valid"],
                f"invalid OKF at {path}: {validation['errors']}",
            )
            self.assertIsInstance(parsed["frontmatter"]["version"], int)
            self.assertGreaterEqual(parsed["frontmatter"]["version"], 1)
            # No leftover temp files from interrupted atomic writes.
            parent = os.path.dirname(path)
            leftovers = [
                n for n in os.listdir(parent) if n.endswith(".tmp") or n.startswith(".")
            ]
            self.assertEqual(leftovers, [], f"leftover temps in {parent}: {leftovers}")


if __name__ == "__main__":
    unittest.main()
