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

On Windows, `npm` and `npx` run as `node <npm dir>/bin/npm-cli.js` (or `npx-cli.js`) from the Node installation found on the scrubbed `PATH`, because their `.cmd` shims would need a shell (EXE-09). Every other `.cmd` or `.bat` stays refused. npm reads its cache and config locations from the environment, so a Node repo usually needs them in its `.arpeggio/config.toml`:

```toml
[repo]
check_env = ["APPDATA", "LOCALAPPDATA"]
```

### Self-check (`arpeggio eval check`)

Every task must pass a self-check before it counts. A task that does not discriminate measures nothing. No model is called.

1. Validate the task file.
2. **Base run**: worktree at `base` on branch `arpeggio/eval/<id>/<run>-base`, then setup, hidden tests, criteria. At least one criterion must fail, else the task is `non_discriminating`.
3. **Solution run**: worktree at `solution`, or at `base` with `reference_diff` applied by the M0.4 patch machinery. Then setup, hidden tests, criteria. Every criterion must pass, else the task is `unsolvable`. A reference diff that does not apply is `unsolvable` too.
4. Both worktrees are removed unless `--keep-worktrees` is given.

Statuses: `valid`, `invalid_schema`, `non_discriminating`, `unsolvable`, `setup_failed`, `error` (an unknown commit, for example). Logs go to `~/.arpeggio/artifacts/eval-check-<run_id>/<task_id>/`. The report, `.reports/check-<UTC timestamp>.json`, holds per task the status, resolved commits, durations, failing criterion numbers (1-based, like `check-<n>.log`) and artifact references. It never holds output. The exit code is `0` only if every selected task is `valid`. Options: `--split tuning|holdout|all`, `--id ID` (repeatable), `--keep-worktrees`, `--json`.

A self-check result is current only while the task file and every file it references (hidden test sources, the reference diff) are older than the report. `eval run` skips a task whose latest report is older than any of them with reason `stale_self_check` and asks for `arpeggio eval check --id <id>`. A fresh clone of the evals repo gives every file a new modification time, so run `eval check` once after cloning.

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

**Text-check tasks.** When the change has no test of its own (docs, config, a template), `eval new` can generate one:

```bash
arpeggio eval new --repo ~/Project/site --base 3f2a9c1 --solution 9b81d07 --id site-footer-year --request "Show the current year in the footer." --check-file resources/views/footer.blade.php --check-pattern "date\(.Y.\)" --category feature --risk low
```

It writes a dependency-free Node test (`node:test`) to `fixtures/<id>/hidden/tests/arpeggio-hidden/<id>.test.mjs` that reads the file and asserts the pattern matches, adds it to `hidden_tests`, and sets `done_criteria` to `node --test tests/arpeggio-hidden/<id>.test.mjs`. Before writing anything it checks the pattern with Python's `re`: it must not match the file at `base` (a missing file does not match) and must match it at `solution`, otherwise the command refuses. At run time the pattern is a JavaScript `RegExp` without flags, so stick to syntax both engines share. The generated file is ASCII with LF line endings and no BOM. `--request`, `--category` and `--risk` also work without a text check. The checks need `node` on the check `PATH`.

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

Reproduce the "Junior → GOAT" comparison on **our own** tasks with **real** prices, so claims are grounded in data rather than simulations. M0.6 runs every strategy through the single-shot patch executor ([ADR-0008](adr/0008-single-shot-patch-executor.md)): agent adapters arrive in v1. Design: [ADR-0010](adr/0010-baseline-runner-and-policy-file.md).

### Strategies

A route is a model key and an effort. Every strategy only sees the models allowed for the task's repo ([route resolution](05-ROUTING-AND-COST.md#route-resolution-for-eval-runs)): the profile, SAF-07 and the placeholder guard decide, and a task with no allowed model is skipped with `no_allowed_route` and a message that says how to opt in. Nothing falls back to a provider the rules exclude.

| Strategy | First route | Escalation |
|---|---|---|
| `junior` | The strongest allowed model at the highest effort it accepts | None |
| `middle` | Per-category map at effort `high` (or the nearest): `docs` and `config` go to the cheapest model of tier1, everything else to the cheapest of tier2 (the nearest tier present when one is missing), `expected_risk: high` to the strongest. `[evals.middle]` in the global config overrides the model per category. **The reference baseline: claims compare against it, never only against `junior`.** | None |
| `senior` | The cheapest model of the lowest tier present, effort `low` | Next tier's cheapest at `medium`, then the strongest at `high` |
| `arpeggio` | First matching rule of the [policy file](05-ROUTING-AND-COST.md#the-policy-file) | The rule's `escalation` list |

At most three attempts per task run. Two consecutive identical routes are merged. Risk is the task's `expected_risk` in M0.6. The signal-based risk classifier arrives in v1, so `arpeggio` here measures the rule table, not risk detection.

Escalation happens only after a verification failure (`checks_failed`), a patch failure (a `patch_*` reason) or `output_truncated`: the reply stopped at the output limit (`finish_reason` `length`) and its patch was missing or did not apply. A gateway that forces thinking on can spend the whole `max_tokens` on reasoning and return empty content, so this is a model failure like a bad patch, not a provider error. A provider error that survives the adapter's own retries ends the task run as `error`. A spend-guard refusal ends it as `paused`. A failing `setup` command ends it as `error` with `setup_failed`, because the task, not the model, is broken. None of these escalate.

### The failure report

An escalated attempt's prompt ends with a short report: the attempt number, the failure kind, each failing check's number and exit code, and the last 1,500 characters of its already redacted output. The report never includes the previous transcript or a hidden test. Output is withheld whenever it could show a hidden test: when the check's arguments name a hidden test path, one of its directories (`tests`, `.`) or its module form (`tests.test_mul`), when they name no path at all (a bare `pytest` or `npm test` may discover hidden tests on its own), or when the output mentions a hidden test file name. The check's number and exit code are still reported.

For `output_truncated` the report adds one fixed line: the reply was cut off at the output limit, keep reasoning short and write the diff first. So escalated prompts of `senior` and `arpeggio` are not identical to first-attempt prompts, beyond the failure report itself: an attempt after a truncation carries this extra instruction, and only those attempts do. Compare first attempts with first attempts when a prompt difference matters.

### Budget, estimate and cap

Eval runs spend money in a loop, so spend is capped before and during the run.

- **Estimate.** Each planned attempt is priced at the spend guard's worst case ([Spend guard](05-ROUTING-AND-COST.md#spend-guard)): prompt tokens `ceil(characters / 2)` plus the prompt overhead, no cache hits, `[evals] max_tokens` output (default 8,192), at the price window in effect now. Prompt characters come from the real prompt: the request, the checks, the context files at `base` (sizes read with `git cat-file`, no worktree) and the system prompt, plus room for the failure report on escalated attempts. A strategy's estimate for a task adds up every route it may try, times `--repeats`, so it is an upper bound.
- **Limit.** `min(--max-usd, eval_per_month_usd − spent on evals this UTC month)`. Spent on evals means the attempt and verification costs of tasks linked to `eval_results`, for tasks created this month. With neither `--max-usd` nor `[budget] eval_per_month_usd`, a real run refuses to start. Under `free`, `eval_per_month_usd` must be 0.
- **Refusal.** A run whose estimate is above the limit does not start.
- **Cap.** Before each attempt, the run checks that the spend so far plus that attempt's worst case stays within the limit. If not, it schedules nothing more, marks every run of this invocation `partial` with reason `budget_cap`, and the report says so. The attempt in flight always finishes. This pre-check is stricter than "stop once the cap is reached", so a run never overshoots its cap by more than the estimate's own error.
- **Peak prices.** When a provider of any planned route is in a peak window now, the run prints the next off-peak start in UTC and local time and refuses to start unless `--allow-peak` is given. Moving runs to off-peak windows automatically is CST-10 (M1.11).

### Running it

```text
arpeggio eval run    [--strategy junior|middle|senior|arpeggio|all] [--split tuning|holdout|all]
                     [--id ID ...] [--repeats N] [--max-usd X] [--dry-run] [--allow-peak]
                     [--keep-worktrees] [--sleep-between S] [--config PATH] [--json]
arpeggio eval report [RUN_ID ...] [--json] [--markdown PATH] [--seed N]
arpeggio eval runs   [--json]
```

Defaults: `--strategy all`, `--split holdout`, `--repeats 1`. Use 3 or more repeats when the budget allows: single runs of a model are noisy. Only tasks whose latest self-check is current and `valid` run. The others are listed as skipped with their status (`stale_self_check`, `unchecked`, `unsolvable`, and so on).

`--dry-run` calls no model and creates no worktree, and it works without API keys. It prints the plan (tasks × strategies × repeats), each strategy's route sequence per task with the matched policy rule, the worst-case estimate, the limit, and any peak warning. Run it before every real run.

A real run creates one `eval_runs` row per strategy and works through the tasks one by one, repeat by repeat, with the strategies interleaved, so a run stopped by the cap still has paired results. Each task run is a `tasks` row (`source = 'eval'`, the profile, the task's category and risk) holding the task's done criteria. Each route tried is one attempt in its own worktree at `base`: the model's diff is applied, the task's `setup` commands run, the hidden tests are copied in, and the checks run. Worktrees are removed after each attempt unless `--keep-worktrees`. `--sleep-between` waits between attempts, for providers with tight per-minute limits.

`--config PATH` uses that file as the global config for one run instead of `~/.arpeggio/config.toml`. Repo-level `.arpeggio/config.toml` still merges on top. The file is read, never written. Its profile and a hash of it are stored on each run, so runs under different profiles can share one database and one report:

```bash
arpeggio eval run --split holdout --repeats 3 --config ~/.arpeggio/profiles/free.toml
arpeggio eval run --split holdout --repeats 3 --config ~/.arpeggio/profiles/micro-deepseek.toml --max-usd 0.50
arpeggio eval report
```

### Privacy opt-in and a first run on Windows

An eval run sends your repository's code to the providers it routes to. Under SAF-07 a `private` repo reaches a provider whose `data_use` is `may_train` or `unknown` (DeepSeek in the `micro-deepseek` template) only after an opt-in. The simplest deliberate opt-in for an eval run is the global one, in the config file you pass with `--config` (or in `~/.arpeggio/config.toml`):

```toml
[privacy]
allow_training_providers = true
```

It applies to `private` repos that leave the repo-level key unset. A `client` repo ignores it. To opt in one repo only, add `allow_training_providers = true` under `[repo]` in that repo's existing `.arpeggio/config.toml` with a text editor. That file also holds `check_env` and other settings, so edit it rather than replacing it. If it does not exist yet, create it with a `[repo]` table.

The commands below work in Windows PowerShell 5.1 and PowerShell 7. They read the API key without echoing it and keep it out of the shell history. Unless `arpeggio` is on your `PATH`, run it from the checkout with `uv run --project`:

```powershell
$arp = "C:\path\to\arpeggio"     # your Arpeggio checkout
$secure = Read-Host "DeepSeek API key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }

uv run --project $arp arpeggio eval check
uv run --project $arp arpeggio eval run --split all --dry-run --config "$env:USERPROFILE\.arpeggio\config.toml"
uv run --project $arp arpeggio eval run --split all --config "$env:USERPROFILE\.arpeggio\config.toml" --max-usd 0.10
uv run --project $arp arpeggio eval report
Remove-Item Env:DEEPSEEK_API_KEY
```

### Recording

`eval_runs` holds the strategy, split, profile, Arpeggio's `git_sha`, the `config_hash` of the effective global config, the repeat count, the planned task runs, the estimate, and the status (`running`, `completed`, `partial` or `aborted`) with its reason. `eval_results` holds one row per task and repeat: status (`solved`, `failed`, `error`, `paused`, `skipped`), the last failure reason or skip reason, cost (attempts plus verification, failed attempts included), attempts, escalations, wall-clock seconds, quota wait (0 until M1.11), the estimate, the counterfactual cost and the task's tags. Each attempt's `route_reason` names the strategy, the step, the requested effort and, for `arpeggio`, the policy rule. The counterfactual cost (CST-05) prices the task's model-call tokens on `[defaults] counterfactual_route` at each call's time and is also stored on `tasks.counterfactual_usd`.

### Report

`arpeggio eval report` takes run IDs, or by default the latest run per profile, split and strategy. It prints Rich tables and writes Markdown to `--markdown PATH`, by default `<evals dir>/.reports/baseline-<UTC timestamp>.md`. A path inside the Arpeggio checkout is refused: reports describe private tasks. `--json` prints the same data as one object.

Sections are per profile and split. Holdout is labeled the headline. Tuning, and a mix of both, are labeled "not for claims". Per strategy: task runs, distinct tasks, solved, success rate, total cost, **cost per solved task**, mean attempts, escalations, wall-clock, quota wait, the share of cost that was estimated rather than reported by the provider, model mismatches, counterfactual cost and savings against it, and the skipped tasks with reasons. A second table counts attempts (not task runs, so an escalated task run can add several) per outcome: `output_truncated`, `patch_missing`, `patch_does_not_apply` and `checks_failed` (a completed attempt with a failing check). It also gives the reasoning tokens and the reasoning share: reasoning tokens over the output tokens of the model calls whose provider reports `completion_tokens_details.reasoning_tokens`. Calls without that field are left out of both sides, and the share is `n/a` when no call reports it. A partial run is marked `PARTIAL` with its reason, and the section says that some planned task runs never ran.

Each section also breaks success rate and cost per solved task down by task tags: by `check:` tags, by `difficulty:` tags (or by category when no task has one) and by `stack:` tags (or by expected risk). Each group shows its number of tasks, and groups under 5 tasks are marked "indicative only". The Markdown report has one table per breakdown.

### Per-profile baselines

The four strategies run under at least the `free` and `micro` profiles (EVL-06). Per profile, the report gives solved tasks, cost, wall-clock time, and time spent waiting on quotas. Under `free`, `junior` means the strongest free model available, since there is no paid frontier model to use. Run each profile with `--config` into the same database, as above.

Deferring eval runs to the cheapest upcoming window (CST-10) arrives in M1.11. Until then the peak warning above is the guard.

## Statistics

- Agent runs are noisy. Each task runs `--repeats 3` (≥ 5 for gate decisions on small suites).
- Use **paired** comparisons: same tasks, same repeats, baseline vs candidate. The report pairs every strategy with `middle` on the tasks both ran.
- Confidence intervals by bootstrap over tasks: tasks are resampled with replacement and a task's repeats stay together, 10,000 resamples, 95% percentile intervals for success rate and cost per solved task. The seed is printed and stored in the JSON report (`--seed`, default 20261008), so the numbers are reproducible.
- A resample without any solved task has no cost per solved task and is dropped. The report gives the dropped share per strategy and labels the interval **unreliable** when it is above 5%.
- With fewer than 10 tasks a section carries a "too few tasks for reliable conclusions" warning. The numbers are still computed.
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

Gate runs cost money. Budget them explicitly with `[budget] eval_per_month_usd` ([Budget, estimate and cap](#budget-estimate-and-cap)) and run them through batch APIs where possible.

## Anti-gaming rules

- Agents never see hidden tests or reference diffs. The prompt builder enforces this for hidden test paths (see [Hidden tests](#hidden-tests)).
- Holdout tasks never appear in taste, skills, or prompts.
- Any proposal whose rationale cites a holdout task is rejected automatically.
- Reported headline numbers always come from the holdout split.
