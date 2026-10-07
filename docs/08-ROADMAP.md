# 08 — Roadmap

Durations assume one developer working part-time. They are estimates, not commitments; each version ends on its **exit criteria**, not on a date.

The order is deliberate: **measure before optimizing, rules before learning, CLI before dashboard.** Do not start a version before the previous one's exit criteria are met and the tool has been used on real work.

## v0 — Foundations & baseline (≈ 3–4 weeks)

Goal: a store, one adapter, and a real baseline. Nothing smart yet.

| Milestone | Scope | Requirements |
|---|---|---|
| M0.1 Skeleton (done 2026-10-07) | Repo, `uv`, CI (ruff, mypy, pytest), config loader with validation, `arpeggio init` | CFG-01..04, CLI-01 (init) |
| M0.2 Store (done 2026-10-07) | SQLite schema v1, migrations, repositories, artifacts dir | OBS-01, OBS-03 |
| M0.2.5 Budget profiles retrofit | `budget.profile` and the per-profile rules in the config schema, provider and model pricing fields, four packaged templates, `arpeggio init --profile`, migration 0002 (profile, deferral, model provenance, price windows, quota usage), database backup before migrating | CFG-05..10, BUD-01, QTA-04, CLI-05 (`init --profile`), NFR-12 |
| M0.3 First adapter | Direct API adapter (OpenAI-compatible), cost recording per step | EXE-01, CST-01 |
| M0.4 Worktrees + verifier | Worktree per attempt, done-criteria runner, verdicts | EXE-04, VER-01, VER-04 |
| M0.5 Eval suite | ≥ 20 real tasks with hidden tests, tuning/holdout split | EVL-01, EVL-02 |
| M0.6 Baseline | Baseline for the `free` and `micro` profiles: `junior`, `middle`, `senior` strategies and a first comparison report per profile | EVL-03, EVL-06 |

**Exit criteria:** baseline report exists with success rate and cost per solved task for 3 strategies on the holdout set, with confidence intervals.

## v1 — Useful daily driver (≈ 6–8 weeks)

Goal: Arpeggio handles real daily tasks more cheaply than the `middle` baseline, safely.

| Milestone | Scope | Requirements |
|---|---|---|
| M1.1 Claude Code adapter | Headless/SDK adapter, streaming, token or estimated cost | EXE-02, CST-02 |
| M1.2 Intake | Done-criteria derivation, clarifying questions, explicit `--check` | INT-01..04 |
| M1.3 Risk + rule router | Signal-based risk, policy file, escalation, provider fallback, single-provider mode | RTE-01..06, RTE-12 |
| M1.4 Cost governor | Budgets, loop detection, context diet, counterfactual cost | CST-03..06 |
| M1.5 Safety | Approval gate, command policy, secret scanner, privacy classes | SAF-01..06 |
| M1.6 Checkpoint & resume | Resumable attempts, timeouts, step limits | EXE-05, EXE-06 |
| M1.7 Feedback capture | Post-agent diff, interventions, rating | LRN-01 |
| M1.8 CLI complete | `run`, `status`, `approve`, `reject`, `replay`, `stats`, `--json` | CLI-01..03, OBS-02 |
| M1.9 Dashboard v1 | One page: tasks, routes, verdicts, cost, savings, strategy comparison | DSH-01, DSH-02 |
| M1.10 Manual eval gate | `arpeggio eval gate <branch>` run before merging routing/cost changes | EVL-04, LRN-06 |
| M1.11 Quota governor & pricing engine | Free-tier usage tracking, rate-limit headers, wait / switch / pause, cost with cache hits and price windows, deferral to off-peak, `free` and `micro` spending rules, $0 fallbacks for every feature | QTA-01..04, CST-10, CST-11, BUD-02, BUD-03, BUD-04 |
| M1.12 Setup wizard & doctor | `arpeggio setup` wizard, `arpeggio doctor`, timed onboarding walkthrough on `free` | CLI-05 (wizard), CLI-06, NFR-11 |
| M1.13 Gateway support | Actual-model provenance and mismatch flagging, `data_use` enforced per privacy class, gateways as separate providers | RTE-11, SAF-07, SAF-08 |

**Exit criteria:**

- On holdout: cost per solved task ≥ 40% below `middle`, success rate within 5 pp, escape proxy not worse.
- Used for ≥ 4 weeks on real work with ≥ 100 user tasks logged.
- Security release checklist passes.
- NFR-11 onboarding walkthrough passes.
- Free and micro baselines published.

## v2 — Learning workbench (≈ 8–12 weeks)

Goal: Arpeggio improves itself from data, under the eval gate.

| Milestone | Scope | Requirements |
|---|---|---|
| M2.1 More adapters | opencode, Command Code | EXE-03 |
| M2.2 Reflector | Failure classification, proposals queue | LRN-02, LRN-03 |
| M2.3 Taste | Diff-to-taste distillation, vendor-neutral store, export to agent formats | LRN-04 |
| M2.4 Skills | Workflow distillation into reusable skills | LRN-05 |
| M2.5 MCP server | Serve taste, skills, task context to all agents | LRN-07 |
| M2.6 Bandit router | Thompson sampling per context, exploration, cold-start fallback | RTE-07, RTE-08 |
| M2.7 Shadow evaluation | Quality-loss measurement for cheap routes | RTE-09 |
| M2.8 Automatic eval gate | CI runs gate on proposals and core changes; CIs in reports | EVL-04, EVL-05 |
| M2.9 Caching & batch | Stable prefixes, batch jobs for reflection/evals | CST-07, CST-08, CST-09 |
| M2.10 Dashboard v2 | Replay, learning queue, trends, value per profile, remaining free quota, TUI | DSH-03..05, CLI-04, BUD-05, QTA-05 |
| M2.11 Intake v2 | Task splitting, duplicate detection | INT-05, INT-06 |
| M2.12 Escape tracking | Link reverts/fixes to tasks | VER-05, VER-06 |

**Exit criteria:**

- Bandit router beats v1 rules on holdout (cost and success rate, outside noise).
- Shadow-measured quality loss reported and below an agreed threshold.
- Post-agent edits ≥ 25% below v1 baseline.

## v3 — Default way of working (open-ended)

Candidates, prioritized by data from v2 usage:

- Tournament mode (RTE-10).
- Domain packs: LiDAR/geospatial (PDAL, point-cloud QA), ML experiment tracking and comparison, Laravel conventions.
- Container sandbox by default for `client` repos.
- Notifications (task done, approval needed) to phone.
- Multi-machine sync of taste and skills (git remote).

**Exit criteria:** ≥ 80% of daily coding tasks go through Arpeggio for a full month.

## Definition of done (every milestone)

- Requirements in scope have passing tests or eval tasks covering their acceptance criteria.
- Docs updated (this folder, `AGENTS.md` if conventions changed, a new ADR if a decision changed).
- No regression on the eval gate for changes to `routing/`, `cost/`, `verify/`, `intake/`.
- Used on at least one real task by the owner.

## Explicitly deferred

Not before v3, regardless of how interesting: own agent loop, local-model brain, multi-user, SaaS, fine-tuning, voice interface.
