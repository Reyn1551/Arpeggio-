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

### Splits

- `evals/tasks/` — **tuning** set. Used to develop policy, taste, and router.
- `evals/holdout/` — **holdout** set, ≥ 30% of all tasks. Never inspected while tuning; only used by the eval gate and for reported numbers.
- Rotate: when a holdout task is used to debug something, move it to tuning and add a fresh holdout task.

### Task file format (`evals/tasks/<id>.yaml`)

```yaml
id: web-017-email-validation
category: feature
expected_risk: low
repo:
  source: git@github.com:<owner>/<repo>.git   # or local path
  commit: 3f2a9c1                             # state before the change
request: >
  Add server-side email format validation to the registration form.
  Invalid emails must return a validation error on the email field.
done_criteria:
  - kind: command
    cmd: php artisan test --filter=RegistrationEmailValidationTest
    expect_exit: 0
  - kind: command
    cmd: ./vendor/bin/pint --test
    expect_exit: 0
hidden_tests:                                  # copied in only at verification time
  - path: tests/Feature/RegistrationEmailValidationTest.php
    source: evals/fixtures/web-017/RegistrationEmailValidationTest.php
reference_diff: evals/fixtures/web-017/reference.diff   # human solution, for diff-size and taste comparison
tags: [laravel, validation]
notes: Agent should not modify unrelated controllers.
```

`hidden_tests` prevent the agent from "solving" the task by editing the tests it is judged against.

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

- Agents never see hidden tests or reference diffs.
- Holdout tasks never appear in taste, skills, or prompts.
- Any proposal whose rationale cites a holdout task is rejected automatically.
- Reported headline numbers always come from the holdout split.
