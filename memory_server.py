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
    InvalidSessionIdError,
    VersionConflictError,
    delete_memory,
    get_session_checkpoint,
    init_db,
    reconcile_memory_store,
    retrieve_memory,
    search_memories,
    set_session_checkpoint,
    store_memory,
)
from okf_engine import DEFAULT_NAMESPACE

SESSIONS_NAMESPACE = "sessions"
DEFAULT_SESSION_ID = "default"
DEPRECATION_NOTICE = (
    "Deprecated: prefer memory_checkpoint_get / memory_checkpoint_set for "
    "session-scoped checkpoints. memory_get_last / memory_update_last route to "
    "memory/sessions/<session_id>.md and will be removed in a future release."
)

SERVER_INSTRUCTIONS = """
MCP-Memory Agent Continuity Protocol:
1. ON SESSION INITIALIZATION: Call `memory_checkpoint_get` (or legacy `memory_get_last`) for your session_id to inspect where work was last left off.
2. ON MILESTONE / PROGRESS UPDATE: Call `memory_checkpoint_set` (or legacy `memory_update_last`) with summary, active_keys, and next_steps for your session_id.
"""

mcp = FastMCP("MCP-Memory", instructions=SERVER_INSTRUCTIONS)

# Initialize database and reconcile on-disk HITL edits at startup (Spec 03).
init_db()
reconcile_memory_store()


def _require_project_root(project_root: str) -> Optional[str]:
    if not project_root or not project_root.strip():
        return (
            "Error: 'project_root' is required. Please provide the absolute path "
            "to your active project root directory."
        )
    return None


@mcp.tool()
def memory_checkpoint_set(
    project_root: str,
    session_id: str,
    summary: str,
    active_keys: Optional[List[str]] = None,
    next_steps: Optional[List[str]] = None,
) -> str:
    """
    Create or update a session-scoped checkpoint at memory/sessions/<session_id>.md.

    Parameters:
        project_root: REQUIRED. Absolute path to active project root directory.
        session_id: Kebab-case session identifier (also used as the OKF key).
        summary: Brief progress summary for this session.
        active_keys: Memory keys currently in play for this session.
        next_steps: Planned follow-up steps for this session.
    """
    err = _require_project_root(project_root)
    if err:
        return err

    try:
        record = set_session_checkpoint(
            session_id=session_id,
            summary=summary,
            active_keys=active_keys or [],
            next_steps=next_steps or [],
            project_root=project_root,
        )
        return (
            f"Successfully set session checkpoint for '{session_id}'.\n"
            f"File synced to: {record.get('file_path')}\n\n"
            f"OKF Record Payload:\n{record['okf_payload']}"
        )
    except InvalidSessionIdError as e:
        return json.dumps(e.to_dict())
    except VersionConflictError as e:
        return json.dumps(e.to_dict())
    except Exception as e:
        return json.dumps(
            {
                "error_code": "CHECKPOINT_SET_FAILED",
                "session_id": session_id,
                "message": str(e),
            }
        )


@mcp.tool()
def memory_checkpoint_get(project_root: str, session_id: str) -> str:
    """
    Retrieve a session-scoped checkpoint from memory/sessions/<session_id>.md.

    Returns JSON null when no checkpoint exists for that session_id.

    Parameters:
        project_root: REQUIRED. Absolute path to active project root directory.
        session_id: Kebab-case session identifier.
    """
    err = _require_project_root(project_root)
    if err:
        return err

    try:
        record = get_session_checkpoint(
            session_id=session_id,
            project_root=project_root,
        )
        if record is None:
            return json.dumps(None)
        return record["raw_payload"]
    except InvalidSessionIdError as e:
        return json.dumps(e.to_dict())
    except Exception as e:
        return json.dumps(
            {
                "error_code": "CHECKPOINT_GET_FAILED",
                "session_id": session_id,
                "message": str(e),
            }
        )


@mcp.tool()
def memory_get_last(
    project_root: str,
    namespace: str = DEFAULT_NAMESPACE,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """
    AGENT DIRECTIVE - SESSION INITIALIZATION (DEPRECATED):
    Prefer `memory_checkpoint_get`. Retrieves memory/sessions/<session_id>.md
    (default session_id='default').

    Parameters:
        project_root: REQUIRED. Absolute path to active project root directory.
        namespace: Unused for routing (kept for call compatibility). Storage is always 'sessions'.
        session_id: Session checkpoint key (default: 'default').
    """
    err = _require_project_root(project_root)
    if err:
        return err

    # namespace retained for backward-compatible signatures only.
    _ = namespace

    try:
        record = get_session_checkpoint(
            session_id=session_id,
            project_root=project_root,
        )
        if not record:
            return (
                f"No previous session checkpoint found for session_id '{session_id}'.\n"
                f"Agent Directive: Start fresh, and update via `memory_checkpoint_set` "
                f"(or legacy `memory_update_last`) when accomplishing milestones.\n"
                f'"deprecation_notice": "{DEPRECATION_NOTICE}"'
            )
        return (
            f"=== LAST SESSION CHECKPOINT (sessions/{session_id}) ===\n"
            f'"deprecation_notice": "{DEPRECATION_NOTICE}"\n\n'
            f"{record['raw_payload']}"
        )
    except InvalidSessionIdError as e:
        return json.dumps(e.to_dict())
    except Exception as e:
        return f"Error retrieving last memory checkpoint: {str(e)}"


@mcp.tool()
def memory_update_last(
    content: Union[str, Dict[str, Any]],
    project_root: str,
    namespace: str = DEFAULT_NAMESPACE,
    summary: Optional[str] = None,
    session_id: str = DEFAULT_SESSION_ID,
) -> str:
    """
    AGENT DIRECTIVE - MILESTONE & SESSION CHECKPOINTING (DEPRECATED):
    Prefer `memory_checkpoint_set`. Writes memory/sessions/<session_id>.md
    (default session_id='default').

    Parameters:
        content: Brief note or structured dictionary summarizing progress.
        project_root: REQUIRED. Absolute path to active project root directory.
        namespace: Unused for routing (kept for call compatibility). Storage is always 'sessions'.
        summary: Optional one-sentence description of the milestone achieved.
        session_id: Session checkpoint key (default: 'default').
    """
    err = _require_project_root(project_root)
    if err:
        return err

    _ = namespace

    try:
        if isinstance(content, dict):
            summary_text = summary or str(content.get("summary") or content)
            active_keys = list(content.get("active_keys") or [])
            next_steps = list(content.get("next_steps") or [])
            if not active_keys and not next_steps:
                # Preserve opaque dict content as summary body when no structured fields.
                body_summary = summary or json.dumps(content)
                record = set_session_checkpoint(
                    session_id=session_id,
                    summary=body_summary,
                    active_keys=[],
                    next_steps=[],
                    project_root=project_root,
                )
            else:
                record = set_session_checkpoint(
                    session_id=session_id,
                    summary=summary_text,
                    active_keys=active_keys,
                    next_steps=next_steps,
                    project_root=project_root,
                )
        else:
            record = set_session_checkpoint(
                session_id=session_id,
                summary=summary or str(content),
                active_keys=[],
                next_steps=[str(content)] if content else [],
                project_root=project_root,
            )
        return (
            f"Successfully updated last memory checkpoint (sessions/{session_id}).\n"
            f"File synced to: {record.get('file_path')}\n"
            f'"deprecation_notice": "{DEPRECATION_NOTICE}"\n\n'
            f"OKF Record Payload:\n{record['okf_payload']}"
        )
    except InvalidSessionIdError as e:
        return json.dumps(e.to_dict())
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
