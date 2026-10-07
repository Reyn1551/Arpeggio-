# 01 — Vision & Goals

## Vision

One workbench where every coding agent and model works for one person: at the lowest possible cost, with verified quality, and with capability that keeps rising because it learns from that person's real work, at any budget, including zero.

The test sentence: **every month, Arpeggio completes more tasks correctly, more cheaply, and with less human intervention than the month before, and proves it with numbers.**

## User

The primary user is a single developer (the repo owner) who works across several domains:

- Web (Laravel, frontend).
- Machine learning and computer vision (training, evaluation, experiments).
- Geospatial and LiDAR data processing (Python pipelines, PDAL).
- Technical and academic documents.

The design must not lock Arpeggio into one domain. Domain-specific behavior enters through **domain packs**.

## Who it is for

The owner comes first. The second audience is students and developers in the international community who have $0 to $5 a month to spend on models. For them the free and micro profiles have to be complete, not trial versions of the paid ones.

## Goals

| ID | Goal | Success measure |
|---|---|---|
| G1 | **Save money without losing quality.** Route each task to the cheapest model + effort combination that still passes verification. | Cost per solved task drops against baseline while success rate stays within tolerance. |
| G2 | **No silent failures.** Every task has machine-checkable done criteria. | Escape rate (bugs found after acceptance) is tracked and declines over time. |
| G3 | **Multi-agent, multi-vendor.** Claude Code, opencode, Command Code, and direct APIs behind one interface. | ≥ 2 adapters in v1, ≥ 4 in v2. Switching provider requires no core code change. |
| G4 | **Learn from real work.** Routing, taste, and skills improve from data, not guesses. | Every learned change passes the eval gate and improves the holdout score. |
| G5 | **Taste belongs to the user.** Style preferences are extracted from real diffs and exported to every agent. | Post-agent edits (lines the user still changes) decline over time. |
| G6 | **Transparent and auditable.** Every task can be replayed step by step, with its cost. | 100% of attempts have a trace and a recorded cost. |
| G7 | **Safe by default.** Agents cannot take irreversible actions without approval. | Zero unapproved destructive actions in the logs. |
| G8 | **Useful at every budget.** Free, micro, standard, and pro profiles all run the full pipeline. | Each profile has a measured baseline on the holdout set. A new user on the free profile reaches a first verified task in ≤ 15 minutes with zero spend. |

## Non-goals

Deliberately **out of scope**, at least until v3:

- **Building an agent loop from scratch** to compete with Claude Code and similar tools. Existing agents are executors. Exception: the "direct API" adapter for simple tasks.
- **Training or fine-tuning models.** All learning happens in the harness layer (rules, taste, skills, routing).
- **Optimizing for local models.** Local models are supported as an OpenAI-compatible provider (useful for the free profile) but are not tuned for.
- **Multi-user product or SaaS.** Arpeggio is a personal tool. The design should not preclude this, but does not optimize for it.
- **Fully autonomous operation.** Merging to the main branch, deploying, and destructive actions always require approval.
- **Circumventing subscription limits or provider terms of service.**
- **Circumventing free-tier or subscription limits**: multi-account or multi-key rotation for the same provider, reverse-engineered or MITM endpoints, scraping web UIs, or reusing subscription credentials outside official clients.

## Core metrics

Full definitions live in [06-EVALUATION.md](06-EVALUATION.md).

| Metric | Meaning | Direction |
|---|---|---|
| Success rate | Share of tasks that pass verification and are accepted | Up |
| Cost per solved task | Total cost (including failed attempts, verification, routing overhead) divided by solved tasks | Down |
| Escape rate | Share of accepted tasks found faulty within 14 days | Down |
| Interventions per task | How often the user had to step in | Down |
| Post-agent edits | Lines the user changed after the agent finished, before merge | Down |
| Counterfactual savings | Cost if everything went to frontier minus actual cost | Up |

**North star:** cost per solved task, with success rate and escape rate as guardrails. A saving that raises escape rate counts as a failure.

## Targets per version

Absolute numbers are set only after the baseline is measured in v0. Until then, targets are relative to the **"Middle" baseline** (model picked by hand, high effort), not the "Junior" baseline (everything on the most expensive model). Beating Junior is trivial and produces misleading claims.

| Version | Target |
|---|---|
| v0 | Measured baseline: 4 strategies run on the eval suite; every core metric has a number. |
| v1 | Cost per solved task ≥ 40% below Middle; success rate drops ≤ 5 percentage points; escape rate does not rise. Free and micro profiles each have a measured baseline report. |
| v2 | Data-driven router beats the v1 rule router on holdout for both cost and success rate. Post-agent edits ≥ 25% below v1. Free-profile success rate on holdout reported and tracked against v1. |
| v3 | Arpeggio is the user's default way of working with AI: ≥ 80% of daily coding tasks go through Arpeggio for a full month. |

## Decision priorities

When in doubt, prioritize in this order:

1. **Safety** (no data loss, no leaked secrets, no ToS violations).
2. **Correctness** (verified results).
3. **Transparency** (auditable).
4. **Cost.**
5. **Speed.**
6. **Convenience and aesthetics.**
