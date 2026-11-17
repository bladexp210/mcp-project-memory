---
name: log-adr
description: Records architectural decisions as OKF v0.3 ADR records in namespace architecture via memory_store. Use when the user asks to log an ADR, capture an architecture decision, or supersede a previous ADR.
---

# Log ADR

Guide a project agent to record Architecture Decision Records (ADRs) as OKF v0.3 Markdown records in `namespace: "architecture"` with `type: "adr"`.

## When to use

- A concrete architecture choice, invariant, or trade-off must be durable and enforceable.
- An older ADR is being replaced and must be marked `superseded`.

## Preconditions

1. Call `memory_search` in `architecture` for active ADRs related to the topic.
2. Call `memory_retrieve` on any ADR you plan to supersede so you have its exact current `version`.
3. Never contradict an active, `verified: true` ADR without explicit user approval.

## Procedure

### 1. Search existing ADRs

- Search for active ADRs (`status: active`, `type: adr`) covering the same decision space.
- If one already matches, update it (with CAS) or stop — do not create a parallel ADR.
- If replacing an older ADR, note its `key` and current `version`.

### 2. Allocate the next key

- Keys are sequential kebab-case: `adr-001-…`, `adr-002-…`, …
- Inspect existing `adr-NNN-*` keys and take the next free integer.
- Example: `adr-004-sqlite-wal-sync`.

### 3. Structure the Markdown body

Every ADR body **must** use these sections:

```markdown
## Context
State the problem, user need, or technical constraint requiring a decision.

## Decision / Specification
State the concrete rule, architecture choice, or contract.

## Consequences & Trade-offs
List downstream impacts, invariants, and accepted risks.
```

### 4. Store the new ADR

Call `memory_store` with:

| Field | Value |
| :--- | :--- |
| `namespace` | `"architecture"` |
| `type` | `"adr"` |
| `key` | `adr-NNN-short-title` |
| `verified` | `false` |
| `updated_by` | `"agent:<id>"` |
| `expected_version` | `0` for a new key |
| `status` | `"active"` (or `"draft"` if still provisional) |
| `stale_after` | ISO-8601 date (e.g. 90 days ahead) |
| `supersedes` | key of the replaced ADR, or omit/`null` if none |
| `schema_version` | `"0.3"` |

### 5. Supersede the previous ADR (when replacing)

If this ADR replaces an older one:

1. Set `supersedes` on the **new** ADR to the old key (e.g. `"adr-002-json-store"`).
2. Update the **old** ADR via `memory_store`:
   - `status: "superseded"`
   - `expected_version` = the old ADR's **exact** current `version` from `memory_retrieve`
   - keep `verified: false` if you are the agent updating it
3. On `VersionConflictError`, re-retrieve the old ADR, merge, and retry once.

## Hard rules (OKF v0.3)

- Always search active ADRs before writing.
- Always use `namespace: "architecture"` and `type: "adr"`.
- Always use sequential `adr-NNN-…` kebab-case keys.
- Always include `## Context`, `## Decision / Specification`, and `## Consequences & Trade-offs`.
- When superseding: set `supersedes` on the new record **and** update the old record's `status` to `"superseded"` with its exact `expected_version`.
- Never set `verified: true` as an agent.
