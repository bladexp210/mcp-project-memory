---
name: triage-stale-memory
description: Scans project memory for stale or unverified OKF v0.3 records and runs a human-in-the-loop triage. Use when the user asks to review stale memory, verify records, clean up deprecated facts, or refresh stale_after dates.
---

# Triage Stale Memory

Guide a project agent to find OKF v0.3 records that need human review and resolve them only with explicit confirmation in the current turn.

## When to use

- Periodic memory hygiene / HITL review.
- User asks to verify, refresh, or deprecate stale or unverified records.

## Scope

- Scan under `memory/` for Markdown OKF records.
- **Include** records where:
  - `stale_after` is **before** today's date (ISO-8601 `YYYY-MM-DD`), or
  - `verified: false`
- **Exclude** everything under `memory/sessions/` (session checkpoints are ephemeral).

Use `memory_search` / filesystem reads as available; prefer MCP retrieve/search when the index is authoritative.

## Procedure

### 1. Collect candidates

Build a list of matching records (outside `sessions/`). For each, capture:

- `key`
- `namespace`
- `status`
- `stale_after`
- short `summary` (first meaningful line of the body, or a one-line paraphrase)

### 2. Present a HITL review table

Show a concise table and wait for the human:

| key | namespace | status | stale_after | summary |
| :--- | :--- | :--- | :--- | :--- |
| `…` | `…` | `…` | `…` | `…` |

Ask, for each row (or in batch if the user replies in batch), whether to:

1. **Verify** — set `verified: true` and refresh `stale_after` (typically +90 days), or
2. **Update** — revise content/metadata then leave or set verification per user instruction, or
3. **Deprecate** — set `status: "deprecated"`.

### 3. Apply only confirmed actions

For each record the user explicitly confirmed in **this turn**:

1. `memory_retrieve` to get the exact current `version`.
2. `memory_store` with `expected_version` set to that version.
3. Set `updated_by` appropriately (`human:<username>` when applying a human verification decision; if the agent performs the write after confirmation, still only set `verified: true` when the user explicitly approved verification).
4. On `VersionConflictError`, re-read, merge, retry once.

### 4. Never auto-verify

**Hard rule:** Never set `verified: true` without explicit user confirmation in the current turn. Silence, older chat context, or “looks fine” inference is not confirmation.

## Hard rules (OKF v0.3)

- Exclude `sessions/` from triage.
- Present the HITL table before mutating anything.
- Require explicit per-record (or clearly batched) user confirmation before verify / update / deprecate.
- Use CAS (`expected_version`) on every update.
- Agents must not self-verify: `verified: true` only after human confirmation this turn.
