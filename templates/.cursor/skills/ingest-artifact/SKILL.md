---
name: ingest-artifact
description: Distills a target artifact (PRD, user interview, API spec, or runbook) into atomic OKF v0.3 memory records via memory_store. Use when the user asks to ingest, extract facts from, or load a document into project memory.
---

# Ingest Artifact

Guide a project agent to read one target file and store discrete OKF v0.3 facts in the local `memory/` store through the `mcp-memory` MCP tools.

## When to use

- User points at a PRD, interview transcript, API spec, or runbook and asks to capture lasting facts.
- You need atomic, searchable records instead of one giant dump of the source document.

## Preconditions

1. Confirm the target file path exists and is readable.
2. Orient with `memory/index.md` if present.
3. Call `memory_search` for candidate topics **before** any `memory_store` to avoid duplicates.

## Procedure

### 1. Read and distill

- Read the full target file.
- Extract only durable facts: decisions, user constraints, API contracts, and operational runbooks.
- Do **not** store line-by-line code summaries or ephemeral meeting chatter.
- Split into atomic facts — one concern per record.

### 2. Deduplicate

For each candidate fact:

1. Run `memory_search` with key phrases from the fact (and related namespaces).
2. If an active record already covers the same fact, skip or propose an update instead of creating a duplicate.
3. Only create a new record when no suitable match exists.

### 3. Choose key, namespace, and type

Use kebab-case keys prefixed by type:

| Source kind | `type` | Key prefix | Typical `namespace` |
| :--- | :--- | :--- | :--- |
| User need / story | `story` | `story-` | `discovery` |
| Hard constraint | `constraint` | `constraint-` | `discovery` or `architecture` |
| API / interface contract | `contract` | `contract-` | `contracts` |
| Ops / deploy procedure | `runbook` | `runbook-` | `deployment` |

Examples: `story-checkout-guest`, `constraint-sqlite-wal-only`, `contract-memory-store-cas`, `runbook-prod-rollback`.

### 4. Store via `memory_store`

For **new** records set:

| Field | Value |
| :--- | :--- |
| `schema_version` | `"0.3"` (OKF v0.3) |
| `key` | kebab-case with type prefix |
| `namespace` | one of: `discovery`, `architecture`, `contracts`, `deployment`, `sessions` |
| `type` | matching the prefix |
| `verified` | `false` (agents never self-verify) |
| `updated_by` | `"agent:<id>"` (e.g. `agent:cursor-ingest`) |
| `expected_version` | `0` (create) |
| `status` | `"active"` or `"draft"` as appropriate |
| `stale_after` | ISO-8601 date **90 days** from today |
| `tags` / `sources` | include the source file path in `sources` |

Markdown body should state the atomic fact clearly (short Context / Decision sections when useful).

### 5. Report

List keys created or skipped (with reason). Do not mark any record `verified: true`.

## Hard rules (OKF v0.3)

- Always `memory_search` first; never blind-write duplicates.
- Always `verified: false` and `updated_by: "agent:<id>"` on agent writes.
- Always `expected_version: 0` for brand-new keys.
- Always set `stale_after` to today + 90 days.
- On `VersionConflictError`, re-read, merge, and retry once with the exact current version.
