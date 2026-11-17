#!/usr/bin/env python3
"""Pre-commit validator for OKF v0.3 Markdown memory records."""

import re
import subprocess
import sys
from pathlib import Path
import yaml

MEMORY_DIR = Path("memory")
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
VALID_NAMESPACES = {"discovery", "architecture", "contracts", "deployment", "sessions"}
VALID_STATUSES = {"draft", "active", "superseded", "deprecated"}
KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        raise ValueError("Missing opening YAML frontmatter delimiter '---'")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError("Missing closing YAML frontmatter delimiter '---'")
    data = yaml.safe_load(parts[1])
    if not isinstance(data, dict):
        raise ValueError("Frontmatter must parse to a YAML mapping")
    return data


def get_head_version(rel_path: str) -> int | None:
    res = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    old_meta = parse_frontmatter(res.stdout)
    return int(old_meta.get("version", 0))


def validate_file(path: Path) -> list[str]:
    errors = []
    rel_path = path.as_posix()
    try:
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{rel_path}: {exc}"]

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in meta:
            errors.append(f"{rel_path}: Missing required field '{field}'")
        elif not isinstance(meta[field], expected_type):
            errors.append(
                f"{rel_path}: Field '{field}' must be {expected_type.__name__}"
            )

    if errors:
        return errors

    if meta["schema_version"] != "0.3":
        errors.append(f"{rel_path}: schema_version must be '0.3'")
    if meta["namespace"] not in VALID_NAMESPACES:
        errors.append(f"{rel_path}: Invalid namespace '{meta['namespace']}'")
    if meta["status"] not in VALID_STATUSES:
        errors.append(f"{rel_path}: Invalid status '{meta['status']}'")
    if not KEY_PATTERN.match(meta["key"]):
        errors.append(f"{rel_path}: Key '{meta['key']}' must be kebab-case")
    if path.stem != meta["key"]:
        errors.append(f"{rel_path}: Filename '{path.stem}' must match key '{meta['key']}'")

    # Prevent agents from self-verifying records
    if meta["updated_by"].startswith("agent:") and meta["verified"] is True:
        errors.append(f"{rel_path}: Agents cannot set verified=true")

    # Enforce monotonic version increments against HEAD
    head_ver = get_head_version(rel_path)
    if head_ver is None and meta["version"] != 1:
        errors.append(f"{rel_path}: New file must start at version 1 (got {meta['version']})")
    elif head_ver is not None and meta["version"] <= head_ver:
        errors.append(
            f"{rel_path}: Version {meta['version']} must exceed HEAD version {head_ver}"
        )

    return errors


def main() -> int:
    if not MEMORY_DIR.exists():
        return 0

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    all_errors = []
    for file_str in staged:
        p = Path(file_str)
        if p.parts[:1] == ("memory",) and p.suffix == ".md" and p.name != "index.md":
            all_errors.extend(validate_file(p))

    if all_errors:
        print("OKF v0.3 Memory Validation Failed:", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())