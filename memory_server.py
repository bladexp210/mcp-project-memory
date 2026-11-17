"""
MCP-Memory Server Entrypoint.
Exposes MCP tools for persistent agent memory backed by OKF v0.3 and SQLite.
Includes 'Last Memory' checkpoint protocol for cross-session continuity.
"""

import json
import warnings
warnings.filterwarnings("ignore")

from typing import Any, Dict, List, Optional, Union
from mcp.server.fastmcp import FastMCP
from db import (
    VersionConflictError,
    delete_memory,
    init_db,
    reconcile_memory_store,
    retrieve_memory,
    search_memories,
    store_memory,
)
from okf_engine import DEFAULT_NAMESPACE

LAST_MEMORY_KEY = "last-memory"

SERVER_INSTRUCTIONS = """
MCP-Memory Agent Continuity Protocol:
1. ON SESSION INITIALIZATION: Whenever you start working on a project or open a session, your VERY FIRST ACTION should be to call `memory_get_last` (or retrieve key 'last-memory') to inspect where work was last left off.
2. ON MILESTONE / PROGRESS UPDATE: Whenever you accomplish a milestone, make key structural changes, or pause work, ALWAYS update the last memory checkpoint by calling `memory_update_last` (or storing key 'last-memory') with a brief summary and references to relevant memory concepts.
"""

mcp = FastMCP("MCP-Memory", instructions=SERVER_INSTRUCTIONS)

# Initialize database and reconcile on-disk HITL edits at startup (Spec 03).
init_db()
reconcile_memory_store()


@mcp.tool()
def memory_get_last(
    project_root: str,
    namespace: str = DEFAULT_NAMESPACE,
) -> str:
    """
    AGENT DIRECTIVE - SESSION INITIALIZATION:
    Call this tool FIRST when starting work or opening a project session to retrieve the last recorded session checkpoint ('last-memory') and understand where work was left off.

    Parameters:
        project_root: REQUIRED. Absolute path to active project root directory.
        namespace: OKF namespace (default: 'discovery').
    """
    if not project_root or not project_root.strip():
        return "Error: 'project_root' is required. Please provide the absolute path to your active project root directory."

    try:
        record = retrieve_memory(key=LAST_MEMORY_KEY, namespace=namespace, project_root=project_root)
        if not record:
            return (
                f"No previous '{LAST_MEMORY_KEY}' checkpoint found for this project.\n"
                f"Agent Directive: Start fresh, and remember to update '{LAST_MEMORY_KEY}' via `memory_update_last` when accomplishing milestones."
            )
        return (
            f"=== LAST SESSION CHECKPOINT ({LAST_MEMORY_KEY}) ===\n\n"
            f"{record['raw_payload']}"
        )
    except Exception as e:
        return f"Error retrieving last memory checkpoint: {str(e)}"


@mcp.tool()
def memory_update_last(
    content: Union[str, Dict[str, Any]],
    project_root: str,
    namespace: str = DEFAULT_NAMESPACE,
    summary: Optional[str] = None,
) -> str:
    """
    AGENT DIRECTIVE - MILESTONE & SESSION CHECKPOINTING:
    Call this tool whenever completing a milestone, making key project changes, or pausing work to update the canonical project checkpoint ('last-memory') so future sessions know where work was left off.

    Parameters:
        content: Brief note or structured dictionary summarizing progress and referencing key memory files.
        project_root: REQUIRED. Absolute path to active project root directory.
        namespace: OKF namespace (default: 'discovery').
        summary: Optional one-sentence description of the milestone achieved.
    """
    if not project_root or not project_root.strip():
        return "Error: 'project_root' is required. Please provide the absolute path to your active project root directory."

    try:
        existing = retrieve_memory(
            key=LAST_MEMORY_KEY,
            namespace=namespace,
            project_root=project_root,
        )
        expected_version = 0
        if existing is not None:
            expected_version = int(existing["frontmatter"]["version"])

        record = store_memory(
            key=LAST_MEMORY_KEY,
            content=content,
            tags=["last-memory", "checkpoint", "milestone"],
            namespace=namespace,
            concept_type="checkpoint",
            title="Last Session Checkpoint",
            description=summary or "Last recorded progress checkpoint and active project state.",
            status="active",
            updated_by="agent:mcp-memory",
            verified=False,
            expected_version=expected_version,
            project_root=project_root,
        )
        return (
            f"Successfully updated last memory checkpoint ('{LAST_MEMORY_KEY}').\n"
            f"File synced to: {record.get('file_path')}\n\n"
            f"OKF Record Payload:\n{record['okf_payload']}"
        )
    except VersionConflictError as e:
        return json.dumps(e.to_dict())
    except Exception as e:
        return f"Error updating last memory checkpoint: {str(e)}"


@mcp.tool()
def memory_store(
    key: str,
    content: Union[str, Dict[str, Any]],
    project_root: str,
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
) -> str:
    """
    Stores or updates a persistent memory record in Open Knowledge Format (OKF v0.3).

    Parameters:
        key: Single-segment kebab-case identifier (e.g. 'adr-004-sqlite-wal-sync').
        content: The core information to store (string text or structured dictionary).
        project_root: REQUIRED. Absolute path to active project root directory.
        tags: Optional classification tags for filtering.
        namespace: One of discovery | architecture | contracts | deployment | sessions (default: discovery).
        concept_type: Record type: story | constraint | adr | contract | runbook | checkpoint.
        title: Human-readable display name.
        description: Single-sentence summary of the concept.
        resource: Canonical URI for the underlying asset.
        status: Lifecycle status (draft | active | superseded | deprecated). Default: active.
        stale_after: ISO date ('YYYY-MM-DD') when memory becomes stale.
        sources: Provenance sources list (strings or mappings).
        usage_window: Unused in OKF v0.3 (accepted for call compatibility).
        verified: Boolean verification flag. Agents must not set true.
        generated_by: Legacy alias for updated_by.
        updated_by: Actor 'agent:<id>' or 'human:<username>'.
        version: Unused for CAS; version is assigned from expected_version / disk.
        expected_version: CAS precondition. 0 = create only; >=1 must match disk version.
        supersedes: Optional key of the replaced record.
        runtime/parameters/executor/attester/computation: Unused in OKF v0.3 (accepted for call compatibility).
    """
    if not project_root or not project_root.strip():
        return "Error: 'project_root' is required. Please provide the absolute path to your active project root directory."

    try:
        record = store_memory(
            key=key,
            content=content,
            tags=tags,
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
            version=version,
            expected_version=expected_version,
            supersedes=supersedes,
            runtime=runtime,
            parameters=parameters,
            executor=executor,
            attester=attester,
            computation=computation,
            project_root=project_root,
        )
        return (
            f"Successfully stored memory for key '{key}' in namespace '{namespace}'.\n"
            f"File synced to: {record.get('file_path')}\n\n"
            f"OKF Record Payload:\n{record['okf_payload']}"
        )
    except VersionConflictError as e:
        return json.dumps(e.to_dict())
    except Exception as e:
        return f"Error storing memory: {str(e)}"


@mcp.tool()
def memory_retrieve(
    key: str,
    project_root: str,
    namespace: str = DEFAULT_NAMESPACE,
) -> str:
    """
    Retrieves a specific memory record by key.

    Parameters:
        key: The exact kebab-case memory key to look up (e.g. 'last-memory').
        project_root: REQUIRED. Absolute path to active project root directory.
        namespace: OKF namespace (default: 'discovery').
    """
    if not project_root or not project_root.strip():
        return "Error: 'project_root' is required. Please provide the absolute path to your active project root directory."

    try:
        reconcile_memory_store(project_root=project_root)
        record = retrieve_memory(key=key, namespace=namespace, project_root=project_root)
        if not record:
            return f"Memory record with key '{key}' not found in namespace '{namespace}'."
        return record["raw_payload"]
    except Exception as e:
        return f"Error retrieving memory: {str(e)}"


@mcp.tool()
def memory_search(
    project_root: str,
    query: Optional[str] = None,
    tags: Optional[List[str]] = None,
    namespace: Optional[str] = None,
    limit: int = 10,
) -> str:
    """
    Searches persistent memories matching keywords, tags, or namespace patterns.

    Parameters:
        project_root: REQUIRED. Absolute path to active project root directory.
        query: Search string for full-text search across keys, frontmatter, and content.
        tags: Optional list of tags to filter by.
        namespace: Optional namespace filter.
        limit: Maximum number of results to return (default: 10).
    """
    if not project_root or not project_root.strip():
        return "Error: 'project_root' is required. Please provide the absolute path to your active project root directory."

    try:
        reconcile_memory_store(project_root=project_root)
        results = search_memories(query=query, tags=tags, namespace=namespace, limit=limit, project_root=project_root)
        if not results:
            return "No matching memories found."

        summary_lines = [f"Found {len(results)} memory record(s):\n"]
        for idx, res in enumerate(results, 1):
            tags_str = ", ".join(res["tags"]) if res["tags"] else "none"
            summary_lines.append(
                f"### {idx}. [{res['key']}] (Namespace: {res['namespace']})\n"
                f"- **Tags**: {tags_str}\n"
                f"- **Updated**: {res['updated_at']}\n"
                f"```markdown\n{res['okf_payload']}\n```\n"
            )
        return "\n".join(summary_lines)
    except Exception as e:
        return f"Error searching memories: {str(e)}"


@mcp.tool()
def memory_delete(
    key: str,
    project_root: str,
    namespace: str = DEFAULT_NAMESPACE,
) -> str:
    """
    Removes a specific memory entry.

    Parameters:
        key: The kebab-case key of the memory to remove.
        project_root: REQUIRED. Absolute path to active project root directory.
        namespace: OKF namespace (default: 'discovery').
    """
    if not project_root or not project_root.strip():
        return "Error: 'project_root' is required. Please provide the absolute path to your active project root directory."

    try:
        deleted = delete_memory(key=key, namespace=namespace, project_root=project_root)
        if deleted:
            return f"Successfully deleted memory key '{key}' from namespace '{namespace}'."
        else:
            return f"Memory key '{key}' not found in namespace '{namespace}'."
    except Exception as e:
        return f"Error deleting memory: {str(e)}"


if __name__ == "__main__":
    mcp.run()
