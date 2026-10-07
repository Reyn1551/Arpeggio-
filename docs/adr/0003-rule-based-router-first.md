# ADR-0003: Rule-based router before a learned router

- **Status:** accepted
- **Date:** 2026-10-07

## Context

A learned router (contextual bandit) needs outcome data per task category and route. On day one there is none; a "learning" router would only be guessing, and its mistakes would be hard to tell apart from noise.

## Decision

v1 routes with a declarative, first-match policy file keyed on risk and category. All outcomes are logged in a form the v2 bandit can consume (`v_route_stats`). The bandit replaces rules per context bucket only after it beats them on holdout and has ≥ 10 observations per arm.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Bandit from day one | "Smart" immediately | Cold start, unverifiable behavior |
| LLM-as-router only | Flexible | Costs tokens per task, inconsistent, hard to audit |
| **Rules first (chosen)** | Auditable, cheap, debuggable, produces clean data | Requires manual tuning in v1 |

## Consequences

- Policy changes go through the eval gate like any other change.
- High-risk tasks stay rule-routed permanently.

## Revisit when

≥ 100 logged tasks per major category exist (start v2 bandit work).
