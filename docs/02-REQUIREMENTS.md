# 02 — Requirements

## Conventions

- **ID format:** `<AREA>-<NN>`. IDs are stable; never reuse a deleted ID.
- **Priority:** `P0` = required for v1, `P1` = v2, `P2` = v3 or later.
- **"MUST / SHOULD / MAY"** follow RFC 2119 meaning.
- Each requirement has an acceptance criterion (AC). A requirement is done only when its AC is covered by an automated test or an eval task.

| Area | Code |
|---|---|
| Intake | INT |
| Risk & routing | RTE |
| Cost governor | CST |
| Execution & adapters | EXE |
| Verification | VER |
| Approvals & safety | SAF |
| Storage & observability | OBS |
| Learning (taste, skills, reflection) | LRN |
| Evaluation | EVL |
| CLI | CLI |
| Dashboard | DSH |
| Configuration | CFG |
| Budget profiles | BUD |
| Quotas | QTA |

---

## Functional requirements

### Intake (INT)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| INT-01 | P0 | The system MUST accept a task as free text plus a target repo path. | `arpeggio run "<text>" --repo <path>` creates a `tasks` row with status `intake`. |
| INT-02 | P0 | The system MUST derive **done criteria** for each task: at least one machine-checkable check (test command, lint, file existence, metric threshold). | Every task leaving intake has ≥ 1 entry in `done_criteria`. |
| INT-03 | P0 | If the task is ambiguous or no checkable criterion can be derived, the system MUST ask the user at most 3 clarifying questions before routing. | An ambiguous eval task triggers a question instead of execution. |
| INT-04 | P0 | The user MUST be able to supply done criteria explicitly (`--check "pytest tests/test_x.py"`), which override derived ones. | Explicit checks are stored and used verbatim. |
| INT-05 | P1 | Intake SHOULD split large tasks into subtasks with their own criteria and dependency order. | A multi-part eval task produces ≥ 2 subtasks with a DAG. |
| INT-06 | P1 | Intake SHOULD detect duplicate or near-duplicate tasks from history and surface the previous result. | Re-submitting a solved task shows the prior attempt. |

### Risk & routing (RTE)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| RTE-01 | P0 | Every task MUST receive a risk level `low`, `medium`, or `high` computed from checkable signals (see [05](05-ROUTING-AND-COST.md#risk-classification)). | Risk and the signals that produced it are stored on the task. |
| RTE-02 | P0 | When signal-based risk and model-estimated risk disagree, the system MUST use the higher one. | Unit test with conflicting inputs returns the higher level. |
| RTE-03 | P0 | The router MUST choose a **route**: `(adapter, model, effort, verification_depth)`. | Every attempt row stores all four fields. |
| RTE-04 | P0 | v1 routing MUST be rule-based from a declarative policy file. | Changing the policy file changes routing without code changes. |
| RTE-05 | P0 | Routing MUST respect per-repo provider allowlists (see SAF-06). | A repo restricted to provider A never routes to provider B. |
| RTE-06 | P0 | On provider error, rate limit, or outage, the router MUST fall back to an equivalent-tier route on another provider. When only one provider is configured, see RTE-12. | Simulated 429/5xx triggers fallback; event is logged. |
| RTE-07 | P1 | The router SHOULD learn route choice per task category with a contextual bandit. | Bandit router beats rule router on holdout (EVL-05). |
| RTE-08 | P1 | The router SHOULD explore: a configurable share (default 5–10%) of low-risk tasks use a non-greedy route. | Exploration rate in logs matches config ± 2 pp over 100 tasks. |
| RTE-09 | P1 | The system SHOULD run **shadow evaluation**: a configurable share of cheap-routed tasks is also run on a frontier route, outside the user's flow, and compared. | Shadow results are stored and excluded from user-facing output. |
| RTE-10 | P2 | The router MAY run a **tournament**: the same task on N routes in parallel worktrees, with the verifier choosing the winner. | Tournament mode produces N attempts and one selected result. |
| RTE-11 | P0 | Gateways are providers with `gateway = true`. For every call the system MUST record the model that actually served it (from the response) and flag a mismatch with the requested model. Mismatched attempts are excluded from router learning. | Recorded fixture with a mismatch is flagged and excluded. |
| RTE-12 | P0 | Single-provider operation MUST be fully supported: provider fallback degrades to retry-with-backoff, then pause. Note: until resumable attempts exist (M1.6), exhausted retries end the attempt with status `error`. M1.6 changes this to `paused`. | Integration test with exactly one provider. |

### Cost governor (CST)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| CST-01 | P0 | The system MUST record cost for every model call: input, output, cached tokens, and price at time of call. | 100% of `steps` with model calls have cost fields. |
| CST-02 | P0 | When an adapter cannot report tokens (e.g. subscription CLI), cost MUST be estimated and flagged `estimated=true`. | Estimated costs are visibly marked in CLI and dashboard. |
| CST-03 | P0 | The system MUST enforce budgets per task, per day, and per month. Exceeding a budget pauses the task and asks the user. | Budget breach in a test pauses execution. |
| CST-04 | P0 | The system MUST detect loops: identical tool call repeated 3×, or N steps with no diff progress, or cost above task threshold. Loops pause the task. | Synthetic looping agent is stopped and flagged. |
| CST-05 | P0 | The system MUST compute **counterfactual cost** per task: estimated cost on the default frontier route. | Each completed task stores `counterfactual_cost`. |
| CST-06 | P0 | Context diet: tool outputs above a size threshold MUST be truncated in context and stored in full on disk, with a pointer. | Large outputs do not enter context verbatim. |
| CST-07 | P1 | The system SHOULD keep prompt prefixes stable (system prompt, taste, skills in fixed order) so provider prompt caching can apply. | Cache-hit tokens appear in cost records where providers report them. |
| CST-08 | P1 | Non-urgent jobs (reflection, eval runs, skill distillation) SHOULD use provider batch APIs where available. | Nightly jobs run through batch endpoints when configured. |
| CST-09 | P1 | Cascade escalation SHOULD only be enabled for a task category when measured cheap-route success rate exceeds the break-even threshold. | Policy disables cascade for categories below threshold. |
| CST-10 | P0 | Tasks MAY be `deferrable` (default true for eval runs, reflection, distillation, shadow evaluation). Deferrable work MUST be scheduled into the cheapest upcoming price window when the provider declares peak windows. | Scheduler test with DeepSeek-like windows. |
| CST-11 | P0 | Cost calculation MUST apply cache-hit pricing and the price window in effect at call time. | Fixture tests. |

### Execution & adapters (EXE)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| EXE-01 | P0 | All executors MUST implement one adapter interface (see [03](03-ARCHITECTURE.md#adapter-interface)). | Core code never imports a specific adapter outside the registry. |
| EXE-02 | P0 | v1 MUST ship two adapters: Claude Code (headless / Agent SDK) and direct API (OpenAI-compatible). | Both pass the adapter contract test suite. |
| EXE-03 | P1 | v2 SHOULD add opencode and Command Code adapters. | Both pass the adapter contract test suite. |
| EXE-04 | P0 | Every attempt MUST run in an isolated git worktree on its own branch. | Concurrent attempts never touch the same working directory. |
| EXE-05 | P0 | Long tasks MUST checkpoint state so an interrupted attempt can resume without re-running completed steps. | Killing the process mid-task and resuming finishes without duplicate cost for completed steps. |
| EXE-06 | P0 | Each attempt MUST have a wall-clock timeout and a step limit. | Exceeding either ends the attempt with status `timeout`. |
| EXE-07 | P1 | Execution SHOULD support container sandboxing (Docker/Podman) as an option per repo. | `sandbox = "container"` runs the agent inside a container. |

### Verification (VER)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| VER-01 | P0 | An attempt MUST be judged by running its done criteria, never by the agent's self-report. | An agent claiming success with failing tests is marked `failed`. |
| VER-02 | P0 | Verification depth MUST follow risk: `light` (lint + affected tests), `full` (full test suite + review by a stronger model). Under `free`, `full` depth replaces the model review with the full test suite plus all configured static checks (linters, type checkers). | Depth stored per attempt matches policy for its risk. |
| VER-03 | P0 | On verification failure, the system MUST either escalate (next route in policy) or stop and report, according to policy. | Failed attempt produces either a new attempt or a `needs_user` status. |
| VER-04 | P0 | Verification output (pass/fail per check, logs) MUST be stored. | `verdicts` rows exist for each check. |
| VER-05 | P1 | The model reviewer SHOULD produce a structured review (issues, severity, confidence) rather than free text. | Review JSON validates against schema. |
| VER-06 | P1 | The system SHOULD track post-acceptance outcomes (reverts, fix commits referencing the task) to compute escape rate. | Escape events are linked to the original task. |

### Approvals & safety (SAF)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| SAF-01 | P0 | Destructive or irreversible actions MUST require explicit user approval: deleting files outside the worktree, force push, push to protected branches, database drops/migrations on non-local DBs, deploys, package publishing. | Each listed action in a test is blocked until approved. |
| SAF-02 | P0 | Secrets (API keys, tokens, `.env` values) MUST never be sent to a model. Files matching secret patterns are excluded from context. | Secret scanner test finds zero secrets in recorded prompts. |
| SAF-03 | P0 | Merging an attempt into the user's branch MUST require user approval in v1. | No auto-merge path exists in v1. |
| SAF-04 | P0 | Content from web pages, issues, READMEs, and tool outputs MUST be treated as data. Instructions found there are not executed without user confirmation. | Prompt-injection eval tasks do not trigger the injected action. |
| SAF-05 | P0 | Every approval request and decision MUST be logged with timestamp and actor. | `approvals` rows exist for every gated action. |
| SAF-06 | P0 | Each repo MUST support a provider allowlist and a privacy class (`public`, `private`, `client`). | `client` repos only route to allowlisted providers. |
| SAF-07 | P0 | Repos with privacy class `private` or `client` MUST NOT route to providers whose `data_use` is `may_train` or `unknown`, unless they opt in. A `private` repo opts in with `[repo] allow_training_providers = true`, or, when the repo leaves it unset, through the global `[privacy] allow_training_providers = true`. A `client` repo ignores the global value and opts in only with an explicit repo-level `true`. | Routing test for each class, with the global value on and off and the repo value unset, true and false. A `client` repo never follows the global value. |
| SAF-08 | P0 | A gateway is a separate provider for privacy purposes, with its own `data_use`. The strictest of gateway and upstream applies when the upstream is known. | Test that gateway `data_use` is enforced. |

### Storage & observability (OBS)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| OBS-01 | P0 | All state MUST be stored in a local SQLite database (see [04](04-DATA-MODEL.md)). | Deleting the DB and running `arpeggio init` yields a working empty system. |
| OBS-02 | P0 | Every attempt MUST have a full step trace (model calls, tool calls, outputs or pointers) sufficient for replay. | `arpeggio replay <attempt>` reproduces the step sequence. |
| OBS-03 | P0 | Structured logs MUST be written as JSON lines with task and attempt IDs. | Logs parse as JSON and correlate with DB rows. |
| OBS-04 | P1 | The system SHOULD export metrics (cost, success, latency) per day for trend analysis. | `arpeggio stats --since 30d` prints daily series. |

### Learning (LRN)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| LRN-01 | P0 | The system MUST capture the diff between the agent's final output and what the user actually merges. | `feedback` row with `post_agent_diff` for every merged task. |
| LRN-02 | P1 | A reflector SHOULD classify failures: `missing_context`, `ambiguous_spec`, `wrong_tool`, `model_limit`, `flaky_check`, `other`. | ≥ 90% of failed attempts receive a class. |
| LRN-03 | P1 | The reflector SHOULD propose harness changes (policy, taste, skill, prompt) as **proposals**, never applying them directly. | Proposals appear in a review queue. |
| LRN-04 | P1 | Taste rules SHOULD be distilled from post-agent diffs into a vendor-neutral `taste/` directory, then exported to `CLAUDE.md`, `AGENTS.md`, and other agent formats. | Export produces valid files for each configured agent. |
| LRN-05 | P1 | Successful repeated workflows SHOULD be proposed as reusable skills in `skills/`. | A workflow solved ≥ 3 times yields a skill proposal. |
| LRN-06 | P0 | Every accepted learning change MUST be versioned in git and pass the eval gate (EVL-04). | Learning changes without a passing gate cannot be merged. |
| LRN-07 | P1 | Taste and skills SHOULD be served to all agents through an MCP server so one source of truth applies everywhere. | Two different adapters read the same taste via MCP. |

### Evaluation (EVL)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| EVL-01 | P0 | The repo MUST contain an eval suite of ≥ 20 real tasks with done criteria (see [06](06-EVALUATION.md)). | `evals/tasks/` has ≥ 20 valid task files. |
| EVL-02 | P0 | ≥ 30% of eval tasks MUST be held out and never used for tuning. | Holdout tasks are tagged and excluded from tuning commands. |
| EVL-03 | P0 | The system MUST run the 4-strategy baseline experiment (Junior, Middle, Senior, Arpeggio). | `arpeggio eval run --strategy all` produces a comparison report. |
| EVL-04 | P0 | An **eval gate** MUST run before any policy, taste, skill, or prompt change is accepted, and reject changes that lower holdout score beyond tolerance. | A regressing change is rejected in CI. |
| EVL-05 | P1 | Eval reports SHOULD include confidence intervals; a change is "better" only if the improvement is outside noise. | Reports show CI per metric. |
| EVL-06 | P0 | The baseline experiment MUST run per profile (at least `free` and `micro`). Reports state results per profile and include wall-clock time and time spent waiting on quotas. | Report contains per-profile sections. |

### CLI (CLI)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| CLI-01 | P0 | Commands: `init`, `run`, `status`, `approve`, `reject`, `replay`, `eval`, `stats`, `config`. | Each command has `--help` and a test. |
| CLI-02 | P0 | `run` MUST stream progress: current route, step, running cost. | Streaming output in an integration test. |
| CLI-03 | P0 | All commands MUST support `--json` output for scripting. | JSON output validates against schema. |
| CLI-04 | P1 | A TUI mode SHOULD show live tasks, approval queue, and spend. | `arpeggio tui` renders without errors. |
| CLI-05 | P0 | `arpeggio init --profile <free\|micro-deepseek\|standard\|pro>` writes the matching packaged config template (default `free`). `arpeggio setup` (interactive wizard: choose profile, detect API keys in environment, write and validate config) is P1. | `init --profile` test per template. Each template validates. |
| CLI-06 | P1 | `arpeggio doctor` checks config validity, presence of referenced environment variables, stale `last_verified` data, and (with `--online`) connectivity. It prints what the current profile can and cannot do. | Offline-mode test. |

### Dashboard (DSH)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| DSH-01 | P0 | One page showing: tasks with their route path, verification result, cost, counterfactual cost, and savings. | Page renders from a seeded DB. |
| DSH-02 | P0 | Strategy comparison view: cost per solved task and success rate for each baseline strategy vs Arpeggio. | Matches `arpeggio eval` report numbers. |
| DSH-03 | P1 | Attempt replay view: step-by-step trace with cost per step. | Replay view matches `arpeggio replay`. |
| DSH-04 | P1 | Learning view: pending proposals, eval gate results, taste diff. | Proposals can be approved/rejected from the UI. |
| DSH-05 | P1 | Trend view: core metrics over time. | Charts render daily series. |

### Configuration (CFG)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| CFG-01 | P0 | Global config in `~/.arpeggio/config.toml`; per-repo overrides in `<repo>/.arpeggio/config.toml`. | Repo config overrides global for that repo only. |
| CFG-02 | P0 | Providers, models, tiers, prices, and effort levels MUST be declared in config, not code, including cache-hit prices, price windows, free-tier limits, effort parameters, and `data_use`. | Adding a model requires only config. |
| CFG-03 | P0 | API keys MUST come from environment variables or the OS keychain, never from config files committed to git. | Config loader rejects inline keys. |
| CFG-04 | P0 | Config MUST be validated on load with clear error messages. | Invalid config fails fast with field-level errors. |
| CFG-05 | P0 | Pricing MUST support input, output, and cache-hit input prices per model, plus provider-level peak windows (UTC weekday ranges) with an off-peak multiplier. | Cost calculator returns correct values for peak, off-peak, and cache-hit fixtures. |
| CFG-06 | P0 | `base_url` MUST use `https`, except loopback hosts (`localhost`, `127.0.0.1`, `::1`), which may use `http` (local gateways, Ollama). `api_key` is optional only for loopback providers. | Validation tests for each case. |
| CFG-07 | P0 | Each provider MUST declare `data_use`: `no_training`, `may_train`, or `unknown` (default `unknown`). | Field validated and shown in `config show`. |
| CFG-08 | P0 | Models MAY declare `effort_params`: a mapping from effort level to provider-specific request parameters (e.g. thinking on/off). Keys MUST be a subset of the model's `efforts`. The parameters MUST NOT set `model`, `messages`, `max_tokens` or `stream`, which the adapter owns. | Validation test. |
| CFG-09 | P0 | Each model MUST declare `last_verified` (date) for its price and limit data. | `arpeggio doctor` warns when older than 30 days. |
| CFG-10 | P0 | Models MAY declare `response_model_aliases`: model names a provider may return for this model. A response whose model matches the requested model or an alias is not a mismatch. | Alias match is not flagged, a non-alias is flagged (tested in M0.3). |

### Budget profiles (BUD)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| BUD-01 | P0 | Config MUST declare `budget.profile` as one of `free`, `micro`, `standard`, `pro`. | Profile is validated, stored on every task, and shown by `arpeggio config show`. |
| BUD-02 | P0 | Under `free`, all USD budgets are 0 and the router MUST only choose routes with expected cost $0 (free-tier models, local models). | Integration test: no attempt under `free` has `cost_usd > 0`. |
| BUD-03 | P0 | Under `micro`, the system MUST never exceed `prepaid_balance_usd − reserve_usd`. Before every model call, the worst-case cost (prompt tokens + `max_output_tokens`, at the current price window) is checked against the remaining balance. | A synthetic call that would exceed the remaining balance is blocked and the task pauses. |
| BUD-04 | P0 | Every feature MUST work under every profile. Features that inherently cost money MUST degrade to a documented $0 alternative under `free` (feature matrix in `05-ROUTING-AND-COST.md`). | Integration tests run the full pipeline under `free` with fake providers. |
| BUD-05 | P1 | `arpeggio stats` and the dashboard SHOULD show value per profile: solved tasks per dollar and per free request. | Stats output includes both figures. |

### Quotas (QTA)

| ID | P | Requirement | Acceptance criterion |
|---|---|---|---|
| QTA-01 | P0 | Models MAY declare free-tier limits (`rpm`, `rpd`, `tpm`, `tpd`). The system MUST track usage against them locally. | Usage counters match a sequence of synthetic calls. |
| QTA-02 | P0 | When providers return rate-limit headers, the system MUST parse them and prefer them over configured values. | Header parsing tests with recorded responses. |
| QTA-03 | P0 | On HTTP 429 or exhausted quota, the router MUST either wait (if reset is within `max_quota_wait_s`), switch to another allowed route of equal or lower cost, or pause the task. Under `free` it MUST never switch to a paid route. | One test per branch. |
| QTA-04 | P0 | The system MUST NOT implement or support quota evasion. Config validation MUST reject two providers with the same `kind` and the same normalized `base_url`. | Duplicate-provider config is rejected. |
| QTA-05 | P1 | The dashboard SHOULD show remaining daily free quota per model and a forecast of exhaustion time. | Quota view renders from seeded data. |

---

## Non-functional requirements

| ID | P | Category | Requirement |
|---|---|---|---|
| NFR-01 | P0 | Overhead | Arpeggio's own overhead (intake, routing, bookkeeping) SHOULD stay below 10% of total task cost, measured monthly. |
| NFR-02 | P0 | Latency | Routing decision in < 2 s excluding intake model calls. CLI commands that do not call models respond in < 500 ms. |
| NFR-03 | P0 | Reliability | No lost work: a crash at any point leaves the DB consistent and every attempt resumable or clearly failed. |
| NFR-04 | P0 | Portability | Runs on Linux (primary) and macOS. Windows via WSL. |
| NFR-05 | P0 | Privacy | All data stays local by default. No telemetry. |
| NFR-06 | P0 | Security | No secret ever written to logs, DB, or prompts. |
| NFR-07 | P0 | Maintainability | Core modules have ≥ 80% line coverage; adapters pass a shared contract test suite. |
| NFR-08 | P0 | Extensibility | A new adapter or domain pack can be added without modifying core modules. |
| NFR-09 | P1 | Scale | Handles ≥ 10,000 tasks and ≥ 1,000,000 steps in SQLite without dashboard queries exceeding 1 s. |
| NFR-10 | P0 | Compliance | Adapters use only official interfaces (APIs, SDKs, official CLIs) in ways allowed by each provider's terms. |
| NFR-11 | P0 | Onboarding | A new user on the `free` profile reaches a first verified task in ≤ 15 minutes from install, with zero spend. Checked by a timed walkthrough recorded before the v1 release. |
| NFR-12 | P0 | Data, not code | Prices, limits, and model lists are data; validation defaults for behavioral settings may live in code if every template states them explicitly. Checked by a code search that finds no price or limit literals outside templates and tests, and by a test that every template states `reserve_usd`, `overhead_alert` and `max_quota_wait_s`. |
