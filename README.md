# Arpeggio

> A personal AI workbench: one orchestrator on top of many coding agents and models that picks the cheapest path that is still correct, verifies the result, and learns from every task.

**Status:** v0. Milestones M0.1, M0.2 and M0.2.5 are done: validated config with budget profiles, four profile templates, `arpeggio init --profile` and `arpeggio config`, the SQLite store with migrations and pre-migration backups, and JSON-lines logs.

*Like an arpeggio, which plays a chord one note at a time from the bottom up, Arpeggio plays every task from the cheapest capable model upward, and only climbs when it has to.*

## The problem

Modern coding agents (Claude Code, opencode, Command Code, direct API calls) are very capable. In daily use, four things leak:

1. **Cost leaks.** Easy tasks run on the most expensive model at maximum effort.
2. **Silent failures.** The agent says "done" while tests never ran or the result is wrong.
3. **No cross-vendor memory.** Taste, skills, and lessons from mistakes are locked inside one tool, or lost entirely.
4. **No data.** Nobody knows which agent or model is actually best for which kind of task.

## The answer

Arpeggio does not build a new agent. It is a **brain layer and a learning layer** on top of existing agents:

- **Intake** clarifies the task and sets machine-checkable done criteria.
- **Router + cost governor** choose agent, model, effort, and verification depth from risk and historical data.
- **Verifier** judges results by tests, not by the agent's claims. Failure means escalation.
- **Learning layer** records everything, then updates routing, taste, and skills. Every change passes an eval gate.
- **Dashboard** shows each task's path, cost per solved task, and real savings against a baseline.
- **Budget profiles** let the same pipeline run at $0 (free tiers, local models), on a few dollars of prepaid credit, or on full paid plans.

## Principles

1. **No silent failures.** Every result is verified, every cost is recorded, every decision is auditable.
2. **Measure first, optimize later.** No "smart" feature ships without data and an eval that proves it.
3. **Cost per solved task** is the cost metric, not cost per token.
4. **Humans own irreversible decisions.**
5. **Vendor-neutral.** Taste, skills, and data belong to the user, not to a provider.
6. **Simple first.** Rules before bandits, SQLite before servers, CLI before dashboard.
7. **Useful at every budget.** Features degrade gracefully, and nothing requires a paid plan.

## Documents

| Document | Contents |
|---|---|
| [Vision & goals](docs/01-VISION-AND-GOALS.md) | Vision, goals, non-goals, core metrics, per-version targets |
| [Requirements](docs/02-REQUIREMENTS.md) | Functional and non-functional requirements with IDs and priorities |
| [Architecture](docs/03-ARCHITECTURE.md) | Layers, components, task lifecycle, adapter interface, stack, layout |
| [Data model](docs/04-DATA-MODEL.md) | SQLite schema and table semantics |
| [Routing & cost](docs/05-ROUTING-AND-COST.md) | Risk classification, routing policy, cascade, bandit, budgets |
| [Evaluation](docs/06-EVALUATION.md) | Eval suite format, metric definitions, holdout, eval gate, baseline experiment |
| [Security & privacy](docs/07-SECURITY-AND-PRIVACY.md) | Threat model, sandboxing, secrets, approvals, code privacy |
| [Roadmap](docs/08-ROADMAP.md) | v0 to v3, milestones, exit criteria |
| [ADRs](docs/adr/) | Architecture decision records |
| [AGENTS.md](AGENTS.md) | Rules for coding agents that help build this repo |

## Quick start

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                # install into .venv
uv run arpeggio --install-completion   # optional shell completion
uv run arpeggio init --profile free    # create ~/.arpeggio/, a starter config.toml and arpeggio.db
uv run arpeggio config validate        # check it (--repo PATH also merges a repo's overrides)
```

The other profiles are `micro-deepseek` (a few dollars of DeepSeek credit), `standard` and `pro`. `init` prints which API key variables the chosen profile expects and whether each one is set.

`arpeggio init` writes to `~/.arpeggio/`, or to `$ARPEGGIO_HOME` when that variable is set. Running it again only adds what is missing and brings the database schema up to date. It never deletes data. Fill in `config.toml` (providers, model ids, prices), then run `arpeggio config validate`. Use `arpeggio config show` to print the merged result. Every command accepts `--json` and prints one JSON object for scripts.

### Planned

These commands do not exist yet:

```bash
arpeggio setup                       # wizard: pick a profile, detect API keys, write and check the config
arpeggio doctor                      # what this profile can and cannot do, missing keys, stale prices
arpeggio run "add email validation to the signup form" --repo ~/code/skriptif
arpeggio status                      # active tasks, today's spend, approval queue
arpeggio eval run --strategy all     # 4-strategy baseline experiment on the eval suite
arpeggio dash                        # dashboard at http://localhost:7777
```

## Names

| Where | Name | Why |
|---|---|---|
| Brand / docs | Arpeggio | |
| PyPI distribution | `arpeggio-ai` | `arpeggio` on PyPI is an unrelated parser library |
| Python import | `arpeggio_ai` | Avoids clashing with that library's `arpeggio` import name |
| CLI command | `arpeggio` | Do not alias it to `arp`; that is the standard Linux network tool |

## License

TBD.
