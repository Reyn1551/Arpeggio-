# 06 — Evaluation

Without evaluation, "self-improving" is a slogan. This document defines the eval suite, the metrics, the baseline experiment, and the eval gate that every learned change must pass.

## Eval suite

### Sources

Eval tasks come from **real work**, not synthetic puzzles:

- Tasks the user actually did (re-created from git history at the commit before the change).
- Every task where Arpeggio failed or a bug escaped (added within a week of discovery).
- A few adversarial tasks: ambiguous specs, prompt-injection content in repo files, tasks that should trigger approval.

Target composition for v1 (≥ 20 tasks), grown to ≥ 60 by v2:

| Category | Share |
|---|---|
| Web (Laravel / frontend) | ~30% |
| Python data / geospatial pipelines | ~25% |
| ML / CV experiment code | ~20% |
| Docs and config | ~10% |
| Adversarial (ambiguity, injection, approval-required) | ~15% |

### Where the suite lives

Real tasks come from the owner's private repositories, so their requests, hidden tests and reference diffs must never be committed to the public Arpeggio repo. The **personal eval suite** lives outside it, in the first of:

1. `ARPEGGIO_EVALS_DIR`, if set,
2. `[evals] dir` in the global config ([config reference](05-ROUTING-AND-COST.md#evals-only-in-the-global-config)),
3. `~/.arpeggio/evals/` (`<ARPEGGIO_HOME>/evals/`).

Make that directory its own **private** git repository (`git init` inside it, push only to a private remote). Tasks are then versioned and backed up without ever touching the Arpeggio repo.

```
<evals dir>/
    tasks/<id>.yaml          # tuning split
    holdout/<id>.yaml        # holdout split
    fixtures/<id>/...        # hidden tests and reference diffs
    .reports/                # self-check reports (JSON)
```

The Arpeggio repo ships only a small public sample suite, `evals/sample/`, with three tasks. Its repository is generated at test time by `tests/samplerepo.py` (a tiny `calc` package with deterministic commits), so no nested git repo is committed. Its task files use placeholders such as `${SAMPLE_REPO}` and `${SAMPLE_C0}`, which only the test suite fills in.

### Splits

- `tasks/` is the **tuning** set. Used to develop policy, taste, and router.
- `holdout/` is the **holdout** set, ≥ 30% of all tasks. Never inspected while tuning; only used by the eval gate and for reported numbers. `arpeggio eval list` reports the holdout share and warns below 30% or below 20 tasks (EVL-01, EVL-02).
- A task ID is unique across both splits. An ID found in both is invalid.
- Rotate: when a holdout task is used to debug something, move it to tuning and add a fresh holdout task.

### Task file format (`tasks/<id>.yaml`)

```yaml
id: skriptif-email-validation   # ^[a-z0-9][a-z0-9-]{2,63}$, equal to the file name
category: feature               # feature | bugfix | refactor | docs | config | data_pipeline | ml_experiment | other
expected_risk: low              # low | medium | high
repo:
  path: C:/Users/me/Project/skriptif   # absolute path to a local git repo (URLs are not supported yet)
  base: "3f2a9c1"               # commit before the change; quote SHAs so YAML keeps them strings
  solution: "9b81d07"           # commit with the human solution (optional if reference_diff is given)
request: >
  One or more sentences describing the task as the owner would ask it (10 to 4,000 characters).
setup:                          # optional, run in order in the worktree before the checks
  - argv: ["composer", "install", "--no-interaction"]
    timeout_s: 600              # 1 to 3,600, default 600
done_criteria:                  # at least one, same spec as task criteria (VER-01)
  - kind: command
    argv: ["php", "artisan", "test", "--filter=EmailValidationTest"]
hidden_tests:                   # optional; path relative to the repo, source relative to the evals dir
  - path: tests/Feature/EmailValidationTest.php
    source: fixtures/skriptif-email-validation/hidden/tests/Feature/EmailValidationTest.php
reference_diff: fixtures/skriptif-email-validation/reference.diff   # optional if solution is given
context_files: [app/Http/Requests/RegisterRequest.php]               # optional hint for prompts
tags: [laravel, validation]
notes: free text, never sent to a model
```

Validation lives in `evals/task.py` (Pydantic, unknown fields rejected). `solution` or `reference_diff` is required. `source` and `reference_diff` are relative, stay inside the evals directory after links are resolved, and must exist. A hidden test path never appears in `context_files`. `request` must not still start with `TODO`. `argv` is always a list, never a shell string. Files are read with `yaml.safe_load` only ([ADR-0009](adr/0009-pyyaml-for-eval-tasks.md)).

### Hidden tests

Hidden tests follow the SWE-bench pattern: test files added or changed in the solution commit become hidden tests. A model never sees them. They are copied into the worktree only for verification, **after** any patch is applied and **before** the checks run. A file already at that path is overwritten, and `eval.hidden_test_overwritten` is logged. When an eval task is attached to a patch attempt, the prompt builder refuses any context file whose path is a hidden test path (compared case-insensitively) before reading it, and logs `patch.context_refused_hidden_test`.

### Setup commands

A fresh worktree holds only committed files, so ignored dependency folders such as `vendor/`, `node_modules/` or `.venv/` are missing. `setup` commands (`composer install`, `npm ci`, `uv sync`) run in the worktree before the checks. They get the same environment allowlist, timeouts, output caps, process-tree kill and secret redaction as checks. A step that fails or times out makes the task `setup_failed`.

### Self-check (`arpeggio eval check`)

Every task must pass a self-check before it counts. A task that does not discriminate measures nothing. No model is called.

1. Validate the task file.
2. **Base run**: worktree at `base` on branch `arpeggio/eval/<id>/<run>-base`, then setup, hidden tests, criteria. At least one criterion must fail, else the task is `non_discriminating`.
3. **Solution run**: worktree at `solution`, or at `base` with `reference_diff` applied by the M0.4 patch machinery. Then setup, hidden tests, criteria. Every criterion must pass, else the task is `unsolvable`. A reference diff that does not apply is `unsolvable` too.
4. Both worktrees are removed unless `--keep-worktrees` is given.

Statuses: `valid`, `invalid_schema`, `non_discriminating`, `unsolvable`, `setup_failed`, `error` (an unknown commit, for example). Logs go to `~/.arpeggio/artifacts/eval-check-<run_id>/<task_id>/`. The report, `.reports/check-<UTC timestamp>.json`, holds per task the status, resolved commits, durations, failing criterion numbers (1-based, like `check-<n>.log`) and artifact references. It never holds output. The exit code is `0` only if every selected task is `valid`. Options: `--split tuning|holdout|all`, `--id ID` (repeatable), `--keep-worktrees`, `--json`.

`arpeggio eval list [--split ...] [--json]` shows ID, split, category, risk, tags and the latest self-check status. A footer gives counts per split, the holdout share, and the warnings.

### How to add a task

1. Pick a change you made: the commit before it (`base`) and the commit that finished it (`solution`).
2. Scaffold the task:

   ```bash
   arpeggio eval new --repo ~/Project/skriptif --base 3f2a9c1 --solution 9b81d07 --id skriptif-email-validation
   ```

   Files changed between the two commits that match a test glob become hidden tests. The defaults are `tests/**`, `test/**`, `**/*Test.php`, `**/test_*.py`, `**/*_test.py`, `**/*.spec.*` and `**/*.test.*`, and `--tests-glob` (repeatable) replaces them. Their solution-commit contents go to `fixtures/<id>/hidden/<path>`. The rest of the change is written to `fixtures/<id>/reference.diff`. `--holdout` puts the task in the holdout split, and `--force` overwrites an existing one. Every file written passes through the secret scanner, and only counts per type are printed.
3. Edit `tasks/<id>.yaml`. Replace `request: "TODO: describe the task"` with the request as you would phrase it. Add `done_criteria` (usually the hidden test command) and `setup` if the repo needs dependencies. Set `category` and `expected_risk`.
4. Run `arpeggio eval check --id <id>` until it reports `valid`.
5. Commit the task in your private evals repository.

## Metric definitions

| Metric | Definition |
|---|---|
| **Solved** | All done criteria pass, including hidden tests, on the final attempt; and, for user tasks, the user merged it. |
| **Success rate** | solved / total tasks. |
| **Total cost** | Sum of all attempt costs + verification costs + intake/routing model calls for the task. Shadow attempts are excluded and reported separately. |
| **Cost per solved task** | total cost of all tasks / solved tasks. Failed tasks' costs stay in the numerator on purpose. |
| **Escape rate** | accepted tasks later linked to a revert or fix / accepted tasks, in a 14-day window. |
| **Interventions per task** | Clarifying answers + approvals + manual restarts, per task. |
| **Post-agent edits** | Changed lines between the accepted attempt's diff and the merged diff. |
| **Counterfactual savings** | Σ counterfactual cost − Σ actual cost. |
| **Quality loss (shadow)** | Share of shadow pairs where the frontier route passed and the routed attempt failed. |
| **Overhead share** | (intake + routing + review model costs) / total cost. |

## Baseline experiment: four strategies

Reproduce the "Junior → GOAT" comparison on **our own** tasks with **real** prices, so claims are grounded in data rather than simulations.

| Strategy | Definition |
|---|---|
| `junior` | Everything on `tier3.frontier`, max effort, light verification, no escalation. |
| `middle` | Model chosen per category by a fixed hand-written map, high effort, light verification. **This is the reference baseline.** |
| `senior` | Always start at `tier1`, verify, escalate tier by tier on failure. |
| `arpeggio` | The current Arpeggio policy (rules in v1, bandit in v2) with risk-based verification. |

Command:

```bash
arpeggio eval run --strategy all --split holdout --repeats 3
```

Report (CLI and dashboard): per strategy, success rate, cost per solved task, escalations, retries, escape proxies (hidden test failures), with 95% confidence intervals. Claims compare against `middle`, never only against `junior`.

### Per-profile baselines

The four strategies run under at least the `free` and `micro` profiles (EVL-06). Per profile, the report gives solved tasks, cost, wall-clock time, and time spent waiting on quotas. Under `free`, `junior` means the strongest free model available, since there is no paid frontier model to use.

Eval runs are deferrable (CST-10): when a provider declares peak windows, they run in the cheapest upcoming window.

## Statistics

- Agent runs are noisy. Each task runs `--repeats 3` (≥ 5 for gate decisions on small suites).
- Use **paired** comparisons: same tasks, same repeats, baseline vs candidate.
- Confidence intervals by bootstrap over tasks (10,000 resamples).
- A candidate is **better** only if the CI of the difference excludes zero in the right direction for the primary metric and no guardrail metric regresses beyond tolerance.

## Eval gate

Every proposal (policy, taste, skill, prompt, router weights) and every change to `routing/`, `cost/`, `verify/`, or `intake/` goes through the gate:

1. Run candidate and current `main` on the **holdout** split with the same seeds and repeats.
2. Primary metric: cost per solved task (or the metric the proposal claims to improve).
3. Guardrails (must not regress beyond tolerance):
   - success rate: ≤ 2 pp drop,
   - hidden-test failures among "solved by agent claim": no increase,
   - overhead share: ≤ 10%.
4. Pass → proposal status `accepted`, merged with the eval run ID in the commit message.
5. Fail → `rejected`, with the report attached so the reflector can learn from it.

Gate runs cost money. Budget them explicitly (an `eval_per_month_usd` budget field, planned for M0.6 and not in the config schema yet) and run them through batch APIs where possible.

## Anti-gaming rules

- Agents never see hidden tests or reference diffs. The prompt builder enforces this for hidden test paths (see [Hidden tests](#hidden-tests)).
- Holdout tasks never appear in taste, skills, or prompts.
- Any proposal whose rationale cites a holdout task is rejected automatically.
- Reported headline numbers always come from the holdout split.
