# ADR-0005: Budget profiles

- **Status:** accepted
- **Date:** 2026-10-07

## Context

Arpeggio was designed around paid API access. Many people who would use it have no budget at all, or a few dollars of prepaid credit on one provider (for example $2 on DeepSeek). Free API tiers and local models exist, but their limits are low and change often. If budget is an afterthought, the free path ends up as a broken demo of the paid one. The decision has to land before the first adapter makes network calls, because it shapes config, the schema, and every cost decision after that.

## Decision

Arpeggio has four budget profiles: `free`, `micro`, `standard` and `pro`. Each one runs the full pipeline. A profile only changes which routes exist, how high quality can go, and how fast work finishes.

- Features that cost money degrade to a documented $0 alternative under `free`. The feature matrix in [05-ROUTING-AND-COST.md](../05-ROUTING-AND-COST.md#budget-profiles) is the reference.
- `budget.profile` is part of the config and is validated with per-profile budget rules.
- Every task stores the profile it ran under, so results can be compared per profile.
- Arpeggio ships one config template per profile (`free`, `micro-deepseek`, `standard`, `pro`), and `arpeggio init --profile` writes it.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| A single "cheap mode" flag | Small change | Too coarse: $0 and $2 of credit need different rules (no spend at all, compared with a hard balance) |
| A separate free edition | Free path can be simplified on its own | Forks the product, and the two drift apart |
| **Four profiles in one product (chosen)** | One pipeline, per-profile rules and baselines, data comparable across profiles | Every feature needs a $0 fallback, and tests must cover each profile |

## Consequences

- The router, cost governor and verifier must read the profile. Features without a $0 alternative cannot ship.
- The eval baseline runs per profile (EVL-06), which multiplies eval time.
- Prices and limits live in templates with a `last_verified` date and must be kept current.

## Revisit when

A profile has less than 5% of usage after six months.
