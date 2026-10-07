# ADR-0004: Learning produces proposals, never direct changes

- **Status:** accepted
- **Date:** 2026-10-07

## Context

Self-modifying systems drift: memory bloats, rules conflict, quality degrades silently. Taste learned from rubber-stamped diffs encodes bad habits.

## Decision

The reflector, taste distiller, skill distiller, and router tuner only create **proposals** (git patches with rationale). A proposal is applied only after it passes the eval gate on the holdout set and the user approves it. Every accepted change is a commit referencing its eval run, so it can be reverted.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Auto-apply learned changes | Fast improvement loop | Silent drift, no rollback discipline |
| **Proposals + eval gate + approval (chosen)** | Auditable, reversible, measurable | Slower; eval runs cost money |

## Consequences

- Eval budget must be planned (`eval_per_month_usd`).
- The dashboard needs a proposals queue.

## Revisit when

Gate pass/fail decisions become highly predictable and the user approves > 95% of passing proposals for three consecutive months; then consider auto-accept for low-risk proposal kinds (e.g. formatting taste).
