---
schema_version: "0.3"
key: "adr-004-sqlite-wal-sync"
namespace: "architecture" # discovery | architecture | contracts | deployment | sessions
type: "adr" # story | constraint | adr | contract | runbook | checkpoint
version: 1 # Integer >= 1. Incremented strictly by +1 on every update.
updated_at: "2026-11-17T10:09:00Z" # ISO-8601 UTC timestamp
updated_by: "agent:cursor-architect" # agent:<id> | human:<username>
status: "active" # draft | active | superseded | deprecated
verified: false # Boolean. Set true only by human review or deterministic test.
stale_after: "2027-02-17" # ISO-8601 date triggering HITL review.
supersedes: null # Optional key of replaced record (e.g., "adr-002-json-store").
tags: ["storage", "concurrency", "sqlite"]
sources: ["docs/prd-v1.md", "commit:8f9a2c1"]
---

## Context
State the problem, user need, or technical constraint requiring a decision.

## Decision / Specification
State the concrete rule, architecture choice, or contract.

## Consequences & Trade-offs
List downstream impacts, invariants, and accepted risks.