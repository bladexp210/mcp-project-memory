"""
Tests for setup.py --init-project scaffolding (templates + project layout).
"""

from __future__ import annotations

import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_PY = REPO_ROOT / "setup.py"
TEMPLATES = REPO_ROOT / "templates"

EXPECTED_SKILLS = (
    "ingest-artifact",
    "log-adr",
    "triage-stale-memory",
)
EXPECTED_NAMESPACES = (
    "discovery",
    "architecture",
    "contracts",
    "deployment",
    "sessions",
)


def _run_init(target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SETUP_PY), "--init-project", str(target), *extra],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


def _parse_skill_frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "missing opening YAML frontmatter"
    parts = text.split("---\n", 2)
    assert len(parts) >= 3, "missing closing YAML frontmatter"
    data = yaml.safe_load(parts[1])
    assert isinstance(data, dict), "frontmatter must be a mapping"
    return data


class TestInitProjectLayout(unittest.TestCase):
    def test_init_project_creates_exact_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "proj"
            # Create a fake git hooks dir so hook install is exercised
            hooks = target / ".git" / "hooks"
            hooks.mkdir(parents=True)

            result = _run_init(target)
            self.assertEqual(
                result.returncode,
                0,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

            rule = target / ".cursor" / "rules" / "memory-protocol.mdc"
            self.assertTrue(rule.is_file(), "memory-protocol.mdc missing")
            self.assertEqual(
                rule.read_text(encoding="utf-8"),
                (TEMPLATES / ".cursor" / "rules" / "memory-protocol.mdc").read_text(
                    encoding="utf-8"
                ),
            )

            for skill in EXPECTED_SKILLS:
                skill_path = target / ".cursor" / "skills" / skill / "SKILL.md"
                self.assertTrue(skill_path.is_file(), f"missing skill {skill}")

            script = target / "scripts" / "validate_memory.py"
            self.assertTrue(script.is_file())
            mode = script.stat().st_mode
            self.assertTrue(
                mode & stat.S_IXUSR,
                f"validate_memory.py not executable: mode={oct(mode)}",
            )

            for ns in EXPECTED_NAMESPACES:
                ns_dir = target / "memory" / ns
                self.assertTrue(ns_dir.is_dir(), f"missing memory/{ns}")
                self.assertTrue(
                    (ns_dir / ".gitkeep").is_file(),
                    f"missing memory/{ns}/.gitkeep",
                )

            hook = hooks / "pre-commit"
            self.assertTrue(hook.is_file())
            hook_text = hook.read_text(encoding="utf-8")
            self.assertIn("python3 scripts/validate_memory.py", hook_text)
            self.assertTrue(hook.stat().st_mode & stat.S_IXUSR)

    def test_init_project_second_run_without_force_skips_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "proj"
            first = _run_init(target)
            self.assertEqual(first.returncode, 0, first.stderr)

            rule = target / ".cursor" / "rules" / "memory-protocol.mdc"
            marker = "# USER CUSTOMIZATION — do not overwrite\n"
            rule.write_text(marker + rule.read_text(encoding="utf-8"), encoding="utf-8")

            skill = target / ".cursor" / "skills" / "ingest-artifact" / "SKILL.md"
            skill_marker = "# CUSTOM SKILL\n"
            skill.write_text(skill_marker + skill.read_text(encoding="utf-8"), encoding="utf-8")

            second = _run_init(target)
            self.assertEqual(
                second.returncode,
                0,
                f"second run crashed:\n{second.stdout}\n{second.stderr}",
            )
            self.assertTrue(
                rule.read_text(encoding="utf-8").startswith(marker),
                "existing rule was overwritten without --force",
            )
            self.assertTrue(
                skill.read_text(encoding="utf-8").startswith(skill_marker),
                "existing skill was overwritten without --force",
            )
            # Layout still present
            self.assertTrue((target / "scripts" / "validate_memory.py").is_file())
            for ns in EXPECTED_NAMESPACES:
                self.assertTrue((target / "memory" / ns / ".gitkeep").is_file())


class TestGeneratedSkillFrontmatter(unittest.TestCase):
    def test_template_skills_have_valid_frontmatter_and_okf_v03_rules(self):
        for skill in EXPECTED_SKILLS:
            path = TEMPLATES / ".cursor" / "skills" / skill / "SKILL.md"
            self.assertTrue(path.is_file(), f"template skill missing: {path}")
            text = path.read_text(encoding="utf-8")
            fm = _parse_skill_frontmatter(text)

            self.assertIn("name", fm)
            self.assertIn("description", fm)
            self.assertIsInstance(fm["name"], str)
            self.assertIsInstance(fm["description"], str)
            self.assertTrue(fm["name"].strip())
            self.assertTrue(fm["description"].strip())
            self.assertEqual(fm["name"], skill)

            lower = text.lower()
            self.assertTrue(
                "okf v0.3" in lower or "okf v0.3" in text or "0.3" in text,
                f"{skill}: must reference OKF v0.3",
            )
            # Skill-specific accuracy checks
            if skill == "ingest-artifact":
                self.assertIn("memory_store", text)
                self.assertIn("memory_search", text)
                self.assertIn("verified: false", text)
                self.assertIn("expected_version", text)
                self.assertIn("stale_after", text)
                self.assertTrue(
                    re.search(r"story-|constraint-|contract-|runbook-", text),
                    "ingest-artifact must document type key prefixes",
                )
                self.assertIn("90", text)
            elif skill == "log-adr":
                self.assertIn('namespace: "architecture"', text)
                self.assertIn('type: "adr"', text)
                self.assertIn("adr-001", text)
                self.assertIn("supersedes", text)
                self.assertIn("superseded", text)
                self.assertIn("expected_version", text)
                self.assertIn("## Context", text)
                self.assertIn("## Decision / Specification", text)
                self.assertIn("## Consequences & Trade-offs", text)
            elif skill == "triage-stale-memory":
                self.assertIn("stale_after", text)
                self.assertIn("verified: false", text)
                self.assertIn("sessions/", text)
                self.assertIn("deprecated", text)
                self.assertIn("verified: true", text)
                self.assertTrue(
                    "confirmation" in lower or "confirm" in lower,
                    "triage must require user confirmation before verified:true",
                )


if __name__ == "__main__":
    unittest.main()
