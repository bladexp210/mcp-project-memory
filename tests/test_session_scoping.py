"""
Failing-first tests for Spec 04: Session-Scoped Checkpoints.

Covers memory_checkpoint_set/get, invalid session_id rejection,
concurrent per-session writes, and legacy last-memory deprecation routing.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import db
from memory_server import (
    memory_checkpoint_get,
    memory_checkpoint_set,
    memory_get_last,
    memory_update_last,
)
from okf_engine import KEY_PATTERN, parse_okf


class _SessionFixture(unittest.TestCase):
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


class TestCheckpointSet(_SessionFixture):
    def test_checkpoint_set_creates_sessions_file_with_structured_sections(self):
        result = memory_checkpoint_set(
            project_root=self.project_root,
            session_id="agent-alpha",
            summary="Finished schema migration planning.",
            active_keys=["adr-004-sqlite-wal", "contract-memory-api"],
            next_steps=["implement CAS writes", "add hash sync tests"],
        )
        self.assertNotIn("error_code", result)

        expected_path = os.path.join(
            self.project_root, "memory", "sessions", "agent-alpha.md"
        )
        self.assertTrue(os.path.exists(expected_path), expected_path)

        with open(expected_path, "r", encoding="utf-8") as f:
            raw = f.read()

        parsed = parse_okf(raw)
        fm = parsed["frontmatter"]
        self.assertEqual(fm["namespace"], "sessions")
        self.assertEqual(fm["type"], "checkpoint")
        self.assertEqual(fm["key"], "agent-alpha")
        self.assertEqual(fm["version"], 1)

        body_lower = parsed["body"].lower()
        self.assertIn("## summary", body_lower)
        self.assertIn("finished schema migration planning", body_lower)
        self.assertIn("## active_keys", body_lower)
        self.assertIn("adr-004-sqlite-wal", parsed["body"])
        self.assertIn("contract-memory-api", parsed["body"])
        self.assertIn("## next_steps", body_lower)
        self.assertIn("implement CAS writes", parsed["body"])
        self.assertIn("add hash sync tests", parsed["body"])


class TestCheckpointGet(_SessionFixture):
    def test_checkpoint_get_returns_exact_checkpoint(self):
        memory_checkpoint_set(
            project_root=self.project_root,
            session_id="agent-beta",
            summary="Mid-task checkpoint.",
            active_keys=["story-onboarding"],
            next_steps=["write failing tests"],
        )
        result = memory_checkpoint_get(
            project_root=self.project_root,
            session_id="agent-beta",
        )
        self.assertIsNotNone(result)
        self.assertNotEqual(result, "null")
        self.assertNotEqual(json.loads(result) if result.strip() == "null" else result, None)

        # Accept either raw OKF markdown or a JSON envelope containing it.
        payload = result
        try:
            decoded = json.loads(result)
            if isinstance(decoded, dict) and "okf_payload" in decoded:
                payload = decoded["okf_payload"]
            elif isinstance(decoded, str):
                payload = decoded
        except json.JSONDecodeError:
            payload = result

        parsed = parse_okf(payload)
        self.assertEqual(parsed["frontmatter"]["key"], "agent-beta")
        self.assertEqual(parsed["frontmatter"]["namespace"], "sessions")
        self.assertIn("Mid-task checkpoint.", parsed["body"])
        self.assertIn("story-onboarding", parsed["body"])

    def test_checkpoint_get_returns_null_when_missing(self):
        result = memory_checkpoint_get(
            project_root=self.project_root,
            session_id="never-written",
        )
        self.assertTrue(
            result is None or result == "null" or json.loads(result) is None,
            f"Expected None/null for missing checkpoint, got: {result!r}",
        )


class TestInvalidSessionId(_SessionFixture):
    def _assert_invalid(self, session_id: str) -> Dict[str, Any]:
        set_result = memory_checkpoint_set(
            project_root=self.project_root,
            session_id=session_id,
            summary="should fail",
            active_keys=[],
            next_steps=[],
        )
        get_result = memory_checkpoint_get(
            project_root=self.project_root,
            session_id=session_id,
        )
        for label, raw in (("set", set_result), ("get", get_result)):
            self.assertIsInstance(raw, str, label)
            payload = json.loads(raw)
            self.assertEqual(payload.get("error_code"), "INVALID_SESSION_ID", label)
            self.assertIn("session_id", payload)
        return json.loads(set_result)

    def test_rejects_non_kebab_case_session_id(self):
        for bad in ("Agent_A", "NOT_KEBAB", "has spaces", "UpperCase"):
            with self.subTest(session_id=bad):
                self.assertIsNone(KEY_PATTERN.match(bad))
                self._assert_invalid(bad)

    def test_rejects_path_traversal_session_id(self):
        for bad in ("../etc/passwd", "..", "foo/bar", "sessions/../discovery"):
            with self.subTest(session_id=bad):
                err = self._assert_invalid(bad)
                self.assertEqual(err["error_code"], "INVALID_SESSION_ID")
                # Must not create traversal paths under memory/
                traversal_probe = os.path.join(
                    self.project_root, "memory", "sessions", bad
                )
                self.assertFalse(
                    os.path.exists(traversal_probe),
                    f"Must not create path for invalid session_id: {traversal_probe}",
                )


class TestConcurrentSessionCheckpoints(_SessionFixture):
    def test_two_sessions_write_without_overwrite_or_deadlock(self):
        errors: List[BaseException] = []
        results: Dict[str, str] = {}
        barrier = threading.Barrier(2, timeout=5)

        def writer(session_id: str, summary: str) -> None:
            try:
                barrier.wait()
                results[session_id] = memory_checkpoint_set(
                    project_root=self.project_root,
                    session_id=session_id,
                    summary=summary,
                    active_keys=[f"key-{session_id}"],
                    next_steps=[f"next-{session_id}"],
                )
            except BaseException as exc:  # noqa: BLE001 — capture for assertion
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(writer, "agent-a", "Checkpoint for agent A"),
                pool.submit(writer, "agent-b", "Checkpoint for agent B"),
            ]
            for fut in as_completed(futures, timeout=15):
                fut.result(timeout=15)

        self.assertEqual(errors, [])
        self.assertIn("agent-a", results)
        self.assertIn("agent-b", results)
        self.assertNotIn("error_code", results["agent-a"])
        self.assertNotIn("error_code", results["agent-b"])

        path_a = os.path.join(self.project_root, "memory", "sessions", "agent-a.md")
        path_b = os.path.join(self.project_root, "memory", "sessions", "agent-b.md")
        self.assertTrue(os.path.exists(path_a))
        self.assertTrue(os.path.exists(path_b))

        with open(path_a, "r", encoding="utf-8") as f:
            body_a = f.read()
        with open(path_b, "r", encoding="utf-8") as f:
            body_b = f.read()

        self.assertIn("Checkpoint for agent A", body_a)
        self.assertIn("key-agent-a", body_a)
        self.assertNotIn("Checkpoint for agent B", body_a)
        self.assertIn("Checkpoint for agent B", body_b)
        self.assertIn("key-agent-b", body_b)
        self.assertNotIn("Checkpoint for agent A", body_b)

        parsed_a = parse_okf(body_a)
        parsed_b = parse_okf(body_b)
        self.assertEqual(parsed_a["frontmatter"]["key"], "agent-a")
        self.assertEqual(parsed_b["frontmatter"]["key"], "agent-b")


class TestLegacyLastMemoryDeprecation(_SessionFixture):
    def test_get_last_defaults_to_default_session_without_typeerror(self):
        # Call without session_id — must not raise TypeError.
        result = memory_get_last(project_root=self.project_root)
        self.assertIsInstance(result, str)
        self.assertIn("deprecation_notice", result)

    def test_update_and_get_last_route_to_sessions_default(self):
        update = memory_update_last(
            content="Legacy milestone update.",
            summary="Legacy summary",
            project_root=self.project_root,
        )
        self.assertIn("deprecation_notice", update)
        self.assertIn("memory_checkpoint_set", update)

        expected_path = os.path.join(
            self.project_root, "memory", "sessions", "default.md"
        )
        self.assertTrue(os.path.exists(expected_path), expected_path)

        with open(expected_path, "r", encoding="utf-8") as f:
            raw = f.read()
        parsed = parse_okf(raw)
        self.assertEqual(parsed["frontmatter"]["namespace"], "sessions")
        self.assertEqual(parsed["frontmatter"]["type"], "checkpoint")
        self.assertEqual(parsed["frontmatter"]["key"], "default")

        retrieved = memory_get_last(project_root=self.project_root)
        self.assertIn("deprecation_notice", retrieved)
        self.assertIn("memory_checkpoint_get", retrieved)
        self.assertIn("Legacy", retrieved)

    def test_explicit_session_id_on_legacy_tools(self):
        update = memory_update_last(
            content="Session-scoped legacy write.",
            project_root=self.project_root,
            session_id="agent-legacy",
        )
        self.assertIn("deprecation_notice", update)
        expected_path = os.path.join(
            self.project_root, "memory", "sessions", "agent-legacy.md"
        )
        self.assertTrue(os.path.exists(expected_path))

        got = memory_get_last(
            project_root=self.project_root,
            session_id="agent-legacy",
        )
        self.assertIn("deprecation_notice", got)
        self.assertIn("Session-scoped legacy write", got)


if __name__ == "__main__":
    unittest.main()
