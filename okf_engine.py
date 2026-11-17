"""
OKF (Open Knowledge Format v0.3) Engine for MCP-Memory.
Serializes, parses, and validates memory records according to OKF v0.3
(specs/01-okf-v03-schema.md), aligned with templates/scripts/validate_memory.py.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Any, Dict, List, Optional, Union
import yaml

SCHEMA_VERSION = "0.3"
VALID_NAMESPACES = frozenset(
    {"discovery", "architecture", "contracts", "deployment", "sessions"}
)
VALID_STATUSES = frozenset({"draft", "active", "superseded", "deprecated"})
VALID_TYPES = frozenset(
    {"story", "constraint", "adr", "contract", "runbook", "checkpoint"}
)
KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
UPDATED_BY_PATTERN = re.compile(r"^(agent|human):.+$")

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

DEFAULT_NAMESPACE = "discovery"
DEFAULT_STATUS = "active"
DEFAULT_TYPE = "story"
DEFAULT_UPDATED_BY = "agent:mcp-memory"


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_stale_after() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%d")


def _validate_key(key: str) -> str:
    if not key or not str(key).strip():
        raise ValueError("Memory key cannot be empty.")
    clean = str(key).strip()
    if clean.endswith(".md"):
        clean = clean[:-3]
    if not KEY_PATTERN.match(clean):
        raise ValueError(
            f"Invalid memory key '{key}': must be single-segment kebab-case "
            f"matching {KEY_PATTERN.pattern}."
        )
    return clean


def _validate_namespace(namespace: str) -> str:
    if not namespace or namespace not in VALID_NAMESPACES:
        raise ValueError(
            f"Invalid namespace '{namespace}': must be one of "
            f"{sorted(VALID_NAMESPACES)}."
        )
    return namespace


def serialize_okf(
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
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    version: Optional[int] = None,
    supersedes: Optional[str] = None,
    runtime: Optional[str] = None,
    parameters: Optional[List[Dict[str, Any]]] = None,
    executor: Optional[Dict[str, Any]] = None,
    attester: Optional[Dict[str, Any]] = None,
    computation: Optional[str] = None,
    extra_frontmatter: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Serialize a memory item into an OKF v0.3 Markdown document with YAML frontmatter.
    """
    # Attested-computation and usage_window are intentionally unused in v0.3.
    _ = (usage_window, runtime, parameters, executor, attester, computation, created_at)

    clean_key = _validate_key(key)
    clean_namespace = _validate_namespace(namespace)

    record_type = concept_type or DEFAULT_TYPE
    if record_type not in VALID_TYPES:
        raise ValueError(
            f"Invalid type '{record_type}': must be one of {sorted(VALID_TYPES)}."
        )

    record_status = status or DEFAULT_STATUS
    if record_status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{record_status}': must be one of {sorted(VALID_STATUSES)}."
        )

    actor = updated_by or generated_by or DEFAULT_UPDATED_BY
    if not UPDATED_BY_PATTERN.match(actor):
        raise ValueError(
            f"Invalid updated_by '{actor}': must match 'agent:<id>' or 'human:<username>'."
        )

    ver = 1 if version is None else int(version)
    if ver < 1:
        raise ValueError(f"Invalid version '{ver}': must be an integer >= 1.")

    if verified is None:
        verified_flag = False
    elif isinstance(verified, bool):
        verified_flag = verified
    else:
        raise ValueError("Field 'verified' must be a boolean.")

    if actor.startswith("agent:") and verified_flag is True:
        raise ValueError("Agents cannot set verified=true.")

    now = _get_utc_now_iso()
    u_at = updated_at or now
    t_list = list(tags) if tags is not None else []
    sources_list = list(sources) if sources is not None else []
    stale = stale_after or _default_stale_after()

    frontmatter: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "key": clean_key,
        "namespace": clean_namespace,
        "type": record_type,
        "version": ver,
        "updated_at": u_at,
        "updated_by": actor,
        "status": record_status,
        "verified": verified_flag,
        "stale_after": stale,
        "tags": t_list,
        "sources": sources_list,
    }

    if supersedes is not None:
        frontmatter["supersedes"] = supersedes
    if title:
        frontmatter["title"] = title
    if description:
        frontmatter["description"] = description
    if resource:
        frontmatter["resource"] = resource

    if extra_frontmatter:
        for k, v in extra_frontmatter.items():
            if k not in frontmatter:
                frontmatter[k] = v

    body_text = ""
    if isinstance(content, str):
        body_text = content.strip()
    elif isinstance(content, dict):
        body_lines = ["# Memory Content\n"]
        for k, v in content.items():
            body_lines.append(
                f"- **{k}**: `{json.dumps(v)}`"
                if isinstance(v, (dict, list))
                else f"- **{k}**: {v}"
            )
        body_lines.append("\n```json")
        body_lines.append(json.dumps(content, indent=2))
        body_lines.append("```")
        body_text = "\n".join(body_lines)
    else:
        body_text = str(content).strip()

    yaml_dump = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_dump}\n---\n\n{body_text}\n"


def parse_okf(raw_payload: str) -> Dict[str, Any]:
    """
    Parse an OKF v0.3 Markdown document into a Python dict.
    Returns key, namespace, tags, frontmatter, body, and raw_payload.
    """
    if not raw_payload:
        raise ValueError("Empty OKF payload.")

    lines = raw_payload.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(
            "Invalid OKF document: missing starting YAML frontmatter delimiter '---'"
        )

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        raise ValueError(
            "Invalid OKF document: missing closing YAML frontmatter delimiter '---'"
        )

    yaml_str = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).strip()

    try:
        frontmatter = yaml.safe_load(yaml_str) or {}
    except Exception as e:
        raise ValueError(f"Failed to parse YAML frontmatter: {e}") from e

    if not isinstance(frontmatter, dict):
        raise ValueError("Frontmatter must parse to a YAML mapping")

    key = frontmatter.get("key", "")
    namespace = frontmatter.get("namespace", DEFAULT_NAMESPACE)
    tags = frontmatter.get("tags", [])

    return {
        "key": key,
        "namespace": namespace,
        "tags": tags,
        "frontmatter": frontmatter,
        "body": body,
        "raw_payload": raw_payload,
    }


def validate_okf_conformance(
    raw_payload: str,
    file_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate that an OKF payload conforms to OKF v0.3 rules
    (aligned with templates/scripts/validate_memory.py).
    """
    errors: List[str] = []
    if not raw_payload or not raw_payload.strip():
        return {"valid": False, "errors": ["Empty payload"], "frontmatter": {}}

    lines = raw_payload.splitlines()
    if not lines or lines[0].strip() != "---":
        errors.append("Document missing starting YAML frontmatter delimiter '---'")
        return {"valid": False, "errors": errors, "frontmatter": {}}

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        errors.append("Document missing closing YAML frontmatter delimiter '---'")
        return {"valid": False, "errors": errors, "frontmatter": {}}

    yaml_str = "\n".join(lines[1:end_idx])
    try:
        frontmatter = yaml.safe_load(yaml_str) or {}
    except Exception as e:
        errors.append(f"Invalid YAML frontmatter: {e}")
        return {"valid": False, "errors": errors, "frontmatter": {}}

    if not isinstance(frontmatter, dict):
        errors.append("Frontmatter must parse to a YAML mapping")
        return {"valid": False, "errors": errors, "frontmatter": {}}

    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in frontmatter:
            errors.append(f"Missing required field '{field}'")
        elif not isinstance(frontmatter[field], expected_type):
            errors.append(
                f"Field '{field}' must be {expected_type.__name__}"
            )

    if errors:
        return {"valid": False, "errors": errors, "frontmatter": frontmatter}

    if frontmatter["schema_version"] != SCHEMA_VERSION:
        errors.append(f"schema_version must be '{SCHEMA_VERSION}'")

    if frontmatter["namespace"] not in VALID_NAMESPACES:
        errors.append(f"Invalid namespace '{frontmatter['namespace']}'")

    if frontmatter["status"] not in VALID_STATUSES:
        errors.append(f"Invalid status '{frontmatter['status']}'")

    if frontmatter["type"] not in VALID_TYPES:
        errors.append(f"Invalid type '{frontmatter['type']}'")

    if not KEY_PATTERN.match(frontmatter["key"]):
        errors.append(f"Key '{frontmatter['key']}' must be kebab-case")

    if frontmatter["version"] < 1:
        errors.append(f"Version {frontmatter['version']} must be >= 1")

    updated_by = frontmatter.get("updated_by", "")
    if not isinstance(updated_by, str) or not UPDATED_BY_PATTERN.match(updated_by):
        errors.append(
            f"updated_by '{updated_by}' must match 'agent:<id>' or 'human:<username>'"
        )

    if (
        isinstance(updated_by, str)
        and updated_by.startswith("agent:")
        and frontmatter["verified"] is True
    ):
        errors.append("Agents cannot set verified=true")

    if file_path is not None:
        stem = os.path.splitext(os.path.basename(file_path))[0]
        if stem != frontmatter["key"]:
            errors.append(
                f"Filename '{stem}' must match key '{frontmatter['key']}'"
            )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "frontmatter": frontmatter,
    }


def get_memory_file_path(
    key: str,
    namespace: str = DEFAULT_NAMESPACE,
    base_dir: str = "memory",
) -> str:
    """Resolve ``memory/<namespace>/<key>.md`` under ``base_dir``.

    Keys must be single-segment kebab-case. Namespaces must be one of the
    closed OKF v0.3 set. Path-traversal tokens are rejected.
    """
    def _reject_traversal(parts, label):
        for p in parts:
            if p in (".", ".."):
                raise ValueError(
                    f"Invalid {label}: traversal tokens not allowed."
                )

    clean_key = _validate_key(key)
    clean_namespace = _validate_namespace(namespace)

    key_parts = [p for p in clean_key.replace("\\", "/").split("/") if p]
    _reject_traversal(key_parts, "memory key")
    if len(key_parts) != 1:
        raise ValueError(
            f"Invalid memory key '{key}': must be a single path segment."
        )

    ns_parts = [p for p in clean_namespace.replace("\\", "/").split("/") if p]
    _reject_traversal(ns_parts, "namespace")
    if len(ns_parts) != 1:
        raise ValueError(
            f"Invalid namespace '{namespace}': must be a single path segment."
        )

    candidate = os.path.join(base_dir, clean_namespace, f"{clean_key}.md")

    # Containment check: resolve against an absolute store root.
    root = os.path.realpath(base_dir) if os.path.exists(base_dir) else os.path.abspath(base_dir)
    resolved = os.path.normpath(os.path.join(root, clean_namespace, f"{clean_key}.md"))
    if not (resolved == root or resolved.startswith(root + os.sep)):
        raise ValueError(
            f"Invalid memory key '{key}': resolves outside the memory store."
        )
    return candidate


def update_directory_index(dir_path: str, is_bundle_root: bool = False) -> None:
    """Generate or update index.md for progressive disclosure in OKF bundle directory."""
    if not os.path.exists(dir_path) or not os.path.isdir(dir_path):
        return

    entries = sorted(os.listdir(dir_path))
    concept_files = []
    subdirs = []

    for item in entries:
        if item.startswith(".") or item in ("index.md", "log.md"):
            continue
        full_p = os.path.join(dir_path, item)
        if os.path.isdir(full_p):
            subdirs.append(item)
        elif item.endswith(".md"):
            concept_files.append(item)

    if not concept_files and not subdirs:
        index_file = os.path.join(dir_path, "index.md")
        if os.path.exists(index_file):
            try:
                os.remove(index_file)
            except OSError:
                pass
        return

    dir_name = os.path.basename(os.path.normpath(dir_path)).replace("_", " ").title()
    lines = []

    if is_bundle_root:
        lines.append(f'---\nokf_version: "{SCHEMA_VERSION}"\n---')

    lines.append(f"# {dir_name}\n")

    if subdirs:
        lines.append("## Directories\n")
        for sd in subdirs:
            lines.append(f"* [{sd}/]({sd}/)")
        lines.append("")

    if concept_files:
        lines.append("## Concepts\n")
        for cf in concept_files:
            title = cf[:-3].replace("-", " ").replace("_", " ").title()
            lines.append(f"* [{title}]({cf})")
        lines.append("")

    index_content = "\n".join(lines).strip() + "\n"
    index_file = os.path.join(dir_path, "index.md")
    with open(index_file, "w", encoding="utf-8") as f:
        f.write(index_content)


def append_log_entry(
    base_dir: str,
    action: str,
    key: str,
    details: Optional[str] = None,
) -> None:
    """Append or update a log entry in log.md at the bundle root."""
    if not os.path.exists(base_dir):
        os.makedirs(base_dir, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = os.path.join(base_dir, "log.md")

    title = key.replace("-", " ").replace("_", " ").title() if key else key
    concept_rel_path = f"/{key.lstrip('/')}.md" if not key.endswith(".md") else f"/{key.lstrip('/')}"
    log_bullet = f"* **{action}**: {title} ([{key}]({concept_rel_path}))"
    if details:
        log_bullet += f" - {details}"

    if not os.path.exists(log_file):
        content = f"# Directory Update Log\n\n## {today}\n{log_bullet}\n"
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(content)
        return

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""

    date_heading = f"## {today}"
    if date_heading in existing:
        lines = existing.splitlines()
        new_lines = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and line.strip() == date_heading:
                new_lines.append(log_bullet)
                inserted = True
        updated_content = "\n".join(new_lines) + "\n"
    else:
        if existing.startswith("# "):
            header_end = existing.find("\n\n")
            if header_end != -1:
                header = existing[:header_end]
                rest = existing[header_end:]
                updated_content = f"{header}\n\n## {today}\n{log_bullet}{rest}"
            else:
                updated_content = (
                    f"# Directory Update Log\n\n## {today}\n{log_bullet}\n\n{existing}"
                )
        else:
            updated_content = (
                f"# Directory Update Log\n\n## {today}\n{log_bullet}\n\n{existing}"
            )

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(updated_content)


def sync_memory_to_disk(
    key: str,
    namespace: str,
    okf_payload: str,
    base_dir: str = "memory",
) -> str:
    """Write raw OKF document to disk as .md file, update index.md, and update log.md."""
    file_path = get_memory_file_path(key=key, namespace=namespace, base_dir=base_dir)
    parent_dir = os.path.dirname(file_path)
    os.makedirs(parent_dir, exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(okf_payload)

    curr = parent_dir
    base_abs = os.path.abspath(base_dir)
    while True:
        is_root = os.path.abspath(curr) == base_abs
        update_directory_index(curr, is_bundle_root=is_root)
        if is_root or (curr == parent_dir and curr == base_dir):
            break
        parent_of_curr = os.path.dirname(curr)
        if parent_of_curr == curr or not parent_of_curr.startswith(base_abs):
            break
        curr = parent_of_curr

    append_log_entry(base_dir=base_dir, action="Update", key=key)
    return file_path


def remove_memory_from_disk(
    key: str,
    namespace: str,
    base_dir: str = "memory",
) -> bool:
    """Remove OKF .md file from disk, update parent indexes, and log deletion."""
    file_path = get_memory_file_path(key=key, namespace=namespace, base_dir=base_dir)
    if not os.path.exists(file_path):
        return False

    try:
        os.remove(file_path)
    except OSError:
        return False

    parent_dir = os.path.dirname(file_path)
    curr = parent_dir
    base_abs = os.path.abspath(base_dir)

    while True:
        is_root = os.path.abspath(curr) == base_abs
        update_directory_index(curr, is_bundle_root=is_root)
        if os.path.exists(curr) and curr != base_abs:
            remaining = [
                i
                for i in os.listdir(curr)
                if i not in ("index.md", "log.md") and not i.startswith(".")
            ]
            if not remaining:
                try:
                    index_file = os.path.join(curr, "index.md")
                    if os.path.exists(index_file):
                        os.remove(index_file)
                    os.rmdir(curr)
                except OSError:
                    pass

        if os.path.abspath(curr) == base_abs:
            break
        parent_of_curr = os.path.dirname(curr)
        if parent_of_curr == curr or not parent_of_curr.startswith(base_abs):
            break
        curr = parent_of_curr

    append_log_entry(base_dir=base_dir, action="Deletion", key=key)
    return True
