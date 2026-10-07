# ADR-0002: Python and SQLite, local-first

- **Status:** accepted
- **Date:** 2026-10-07

## Context

Single user, single machine, data that is valuable for learning and must stay private. The owner works mainly in Python.

## Decision

Python 3.12 with `uv`; SQLite in WAL mode as the only store; large payloads as files under `~/.arpeggio/artifacts/`. No server process required except the optional dashboard.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| TypeScript (like several agent CLIs) | Same language as some agent SDKs | Not the owner's main language; weaker data/ML tooling |
| Postgres | Concurrency, richer SQL | Ops overhead for a single-user tool |
| **Python + SQLite (chosen)** | Zero ops, one-file backup, fast enough, great analysis tooling | Write concurrency limits; must keep queries indexed |

## Consequences

- Parallel attempts write through one async DB writer to avoid lock contention.
- Analysis can use pandas directly on the DB.

## Revisit when

Dashboard queries exceed 1 s at expected scale (NFR-09), or multi-machine use becomes a real need.
