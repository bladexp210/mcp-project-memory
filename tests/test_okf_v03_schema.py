"""
Failing-first tests for OKF v0.3 schema (specs/01-okf-v03-schema.md).

Covers required frontmatter fields/types, closed namespaces and statuses,
kebab-case key validation, filename-to-key matching, and the agent verified gate.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from typing import Any, Dict

from okf_engine import (
    get_memory_file_path,
    parse_okf,
    serialize_okf,
    validate_okf_conformance,
)

REQUIRED_FIELDS = {
    "schema_version": str,
    "key": str,
    "namespace": str,
    "type": str,
    "version": int,
    "updated_at": str,
    "updated_by": str,
    "status": str,
    "verified": bool,
    "stale_after": str,
    "tags": list,
    "sources": list,
}

VALID_NAMESPACES = frozenset(
    {"discovery", "architecture", "contracts", "deployment", "sessions"}
)
VALID_STATUSES = frozenset({"draft", "active", "superseded", "deprecated"})
VALID_TYPES = frozenset(
    {"story", "constraint", "adr", "contract", "runbook", "checkpoint"}
)
KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _minimal_serialize(**overrides: Any) -> str:
    """Serialize a valid-by-default OKF v0.3 record; overrides replace defaults."""
    kwargs: Dict[str, Any] = {
        "key": "adr-004-sqlite-wal-sync",
        "content": "## Context\nProblem.\n\n## Decision\nUse WAL.\n",
        "namespace": "architecture",
        "concept_type": "adr",
        "status": "active",
        "stale_after": "2027-02-17",
        "tags": ["storage", "concurrency"],
        "sources": ["docs/prd-v1.md"],
        "updated_by": "agent:cursor-architect",
        "verified": False,
        "version": 1,
    }
    kwargs.update(overrides)
    return serialize_okf(**kwargs)


class TestOKFv03RequiredFieldsAndTypes(unittest.TestCase):
    def test_serialize_emits_all_required_fields_with_correct_types(self):
        payload = _minimal_serialize()
        parsed = parse_okf(payload)
        fm = parsed["frontmatter"]

        for field, expected_type in REQUIRED_FIELDS.items():
            self.assertIn(field, fm, f"missing required field '{field}'")
            self.assertIsInstance(
                fm[field],
                expected_type,
                f"field '{field}' must be {expected_type.__name__}, got {type(fm[field]).__name__}",
            )

        self.assertEqual(fm["schema_version"], "0.3")
        self.assertEqual(fm["version"], 1)
        self.assertIsInstance(fm["verified"], bool)
        self.assertFalse(fm["verified"])

    def test_parse_exposes_v03_top_level_fields(self):
        payload = _minimal_serialize(
            updated_by="human:mohamed",
            verified=True,
            version=3,
        )
        parsed = parse_okf(payload)
        self.assertEqual(parsed["key"], "adr-004-sqlite-wal-sync")
        self.assertEqual(parsed["namespace"], "architecture")
        self.assertEqual(parsed["frontmatter"]["schema_version"], "0.3")
        self.assertEqual(parsed["frontmatter"]["version"], 3)
        self.assertEqual(parsed["frontmatter"]["updated_by"], "human:mohamed")
        self.assertIs(parsed["frontmatter"]["verified"], True)

    def test_validation_rejects_missing_required_field(self):
        payload = _minimal_serialize()
        # Strip schema_version from frontmatter
        lines = payload.splitlines()
        rebuilt = []
        for line in lines:
            if line.startswith("schema_version:"):
                continue
            rebuilt.append(line)
        broken = "\n".join(rebuilt) + "\n"
        result = validate_okf_conformance(broken)
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("schema_version" in err for err in result["errors"]),
            result["errors"],
        )

    def test_validation_rejects_wrong_field_types(self):
        payload = _minimal_serialize()
        broken = payload.replace("version: 1", "version: '1'")
        broken = broken.replace("verified: false", "verified: 'false'")
        result = validate_okf_conformance(broken)
        self.assertFalse(result["valid"])
        joined = " ".join(result["errors"])
        self.assertIn("version", joined)
        self.assertIn("verified", joined)

    def test_default_namespace_is_discovery(self):
        payload = serialize_okf(
            key="story-user-prefers-dark",
            content="Dark mode preferred.",
            stale_after="2027-01-01",
            sources=[],
        )
        parsed = parse_okf(payload)
        self.assertEqual(parsed["namespace"], "discovery")
        self.assertEqual(parsed["frontmatter"]["status"], "active")
        self.assertEqual(parsed["frontmatter"]["schema_version"], "0.3")


class TestOKFv03ClosedNamespaces(unittest.TestCase):
    def test_each_closed_namespace_is_accepted(self):
        for ns in sorted(VALID_NAMESPACES):
            with self.subTest(namespace=ns):
                payload = _minimal_serialize(namespace=ns, key=f"record-{ns}")
                result = validate_okf_conformance(payload)
                self.assertTrue(result["valid"], result["errors"])
                self.assertEqual(parse_okf(payload)["namespace"], ns)

    def test_unknown_namespace_is_rejected(self):
        for bad in ("default", "user_settings", "test_ns", "foo"):
            with self.subTest(namespace=bad):
                with self.assertRaises(ValueError):
                    _minimal_serialize(namespace=bad)
                # Also reject when validating a hand-crafted payload
                crafted = (
                    "---\n"
                    "schema_version: '0.3'\n"
                    "key: adr-004-sqlite-wal-sync\n"
                    f"namespace: {bad}\n"
                    "type: adr\n"
                    "version: 1\n"
                    "updated_at: '2026-11-17T10:09:00Z'\n"
                    "updated_by: agent:cursor-architect\n"
                    "status: active\n"
                    "verified: false\n"
                    "stale_after: '2027-02-17'\n"
                    "tags: []\n"
                    "sources: []\n"
                    "---\n\nBody\n"
                )
                result = validate_okf_conformance(crafted)
                self.assertFalse(result["valid"])
                self.assertTrue(
                    any("namespace" in err.lower() for err in result["errors"]),
                    result["errors"],
                )

    def test_path_rejects_non_closed_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "memory")
            with self.assertRaises(ValueError):
                get_memory_file_path(
                    "adr-001-example",
                    namespace="default",
                    base_dir=base,
                )


class TestOKFv03ClosedStatuses(unittest.TestCase):
    def test_each_closed_status_is_accepted(self):
        for status in sorted(VALID_STATUSES):
            with self.subTest(status=status):
                payload = _minimal_serialize(status=status)
                result = validate_okf_conformance(payload)
                self.assertTrue(result["valid"], result["errors"])
                self.assertEqual(parse_okf(payload)["frontmatter"]["status"], status)

    def test_legacy_stable_status_is_rejected(self):
        with self.assertRaises(ValueError):
            _minimal_serialize(status="stable")
        crafted = (
            "---\n"
            "schema_version: '0.3'\n"
            "key: adr-004-sqlite-wal-sync\n"
            "namespace: architecture\n"
            "type: adr\n"
            "version: 1\n"
            "updated_at: '2026-11-17T10:09:00Z'\n"
            "updated_by: agent:cursor-architect\n"
            "status: stable\n"
            "verified: false\n"
            "stale_after: '2027-02-17'\n"
            "tags: []\n"
            "sources: []\n"
            "---\n\nBody\n"
        )
        result = validate_okf_conformance(crafted)
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("status" in err.lower() for err in result["errors"]),
            result["errors"],
        )


class TestOKFv03ClosedTypes(unittest.TestCase):
    def test_each_closed_type_is_accepted(self):
        for record_type in sorted(VALID_TYPES):
            with self.subTest(type=record_type):
                payload = _minimal_serialize(
                    concept_type=record_type,
                    key=f"{record_type}-example-one",
                )
                result = validate_okf_conformance(payload)
                self.assertTrue(result["valid"], result["errors"])
                self.assertEqual(
                    parse_okf(payload)["frontmatter"]["type"], record_type
                )


class TestOKFv03KebabCaseKeys(unittest.TestCase):
    def test_valid_single_segment_kebab_keys(self):
        for key in (
            "adr-004-sqlite-wal-sync",
            "story-dark-mode",
            "a",
            "contract-api-v1",
            "runbook-deploy-prod",
        ):
            with self.subTest(key=key):
                self.assertRegex(key, KEY_PATTERN)
                payload = _minimal_serialize(key=key)
                result = validate_okf_conformance(payload)
                self.assertTrue(result["valid"], result["errors"])

    def test_slash_separated_and_invalid_keys_rejected(self):
        for bad in (
            "user/preferences/theme",
            "system/last_memory",
            "UpperCase",
            "has_underscore",
            "ends-",
            "-starts",
            "double--dash",
            "",
        ):
            with self.subTest(key=bad):
                if bad:
                    self.assertIsNone(KEY_PATTERN.match(bad))
                with self.assertRaises(ValueError):
                    serialize_okf(
                        key=bad,
                        content="x",
                        namespace="discovery",
                        stale_after="2027-01-01",
                        sources=[],
                    )


class TestOKFv03FilenameToKeyMatching(unittest.TestCase):
    def test_path_is_memory_namespace_key_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "memory")
            key = "adr-004-sqlite-wal-sync"
            namespace = "architecture"
            path = get_memory_file_path(key=key, namespace=namespace, base_dir=base)
            resolved = os.path.realpath(path)
            self.assertTrue(
                resolved.startswith(os.path.realpath(base) + os.sep),
                resolved,
            )
            self.assertEqual(os.path.basename(path), f"{key}.md")
            self.assertEqual(
                os.path.normpath(path),
                os.path.normpath(os.path.join(base, namespace, f"{key}.md")),
            )

    def test_path_stem_always_equals_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "memory")
            for ns in sorted(VALID_NAMESPACES):
                key = f"record-in-{ns}"
                path = get_memory_file_path(key=key, namespace=ns, base_dir=base)
                stem = os.path.splitext(os.path.basename(path))[0]
                self.assertEqual(stem, key)

    def test_validation_rejects_filename_key_mismatch(self):
        payload = _minimal_serialize(key="adr-004-sqlite-wal-sync")
        result = validate_okf_conformance(
            payload,
            file_path="/tmp/memory/architecture/adr-999-other.md",
        )
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("match" in err.lower() or "filename" in err.lower() for err in result["errors"]),
            result["errors"],
        )

    def test_slash_keys_rejected_by_path_resolver(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "memory")
            with self.assertRaises(ValueError):
                get_memory_file_path(
                    "user/preferences/theme",
                    namespace="discovery",
                    base_dir=base,
                )


class TestOKFv03AgentVerifiedGate(unittest.TestCase):
    def test_agent_cannot_set_verified_true(self):
        with self.assertRaises(ValueError):
            _minimal_serialize(
                updated_by="agent:cursor-architect",
                verified=True,
            )
        crafted = (
            "---\n"
            "schema_version: '0.3'\n"
            "key: adr-004-sqlite-wal-sync\n"
            "namespace: architecture\n"
            "type: adr\n"
            "version: 1\n"
            "updated_at: '2026-11-17T10:09:00Z'\n"
            "updated_by: agent:cursor-architect\n"
            "status: active\n"
            "verified: true\n"
            "stale_after: '2027-02-17'\n"
            "tags: []\n"
            "sources: []\n"
            "---\n\nBody\n"
        )
        result = validate_okf_conformance(crafted)
        self.assertFalse(result["valid"])
        self.assertTrue(
            any("verified" in err.lower() for err in result["errors"]),
            result["errors"],
        )

    def test_human_may_set_verified_true(self):
        payload = _minimal_serialize(
            updated_by="human:mohamed",
            verified=True,
        )
        result = validate_okf_conformance(payload)
        self.assertTrue(result["valid"], result["errors"])
        self.assertIs(parse_okf(payload)["frontmatter"]["verified"], True)

    def test_agent_with_verified_false_is_accepted(self):
        payload = _minimal_serialize(
            updated_by="agent:cursor-architect",
            verified=False,
        )
        result = validate_okf_conformance(payload)
        self.assertTrue(result["valid"], result["errors"])


if __name__ == "__main__":
    unittest.main()
