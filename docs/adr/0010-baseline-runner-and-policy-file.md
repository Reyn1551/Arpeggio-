# ADR-0010: Baseline runner and the rule policy file

- **Status:** accepted
- **Date:** 2026-10-08

## Context

Milestone M0.6 produces v0's exit criterion: four strategies (`junior`, `middle`, `senior`, `arpeggio`) compared on the owner's eval tasks with real prices, per profile, with confidence intervals ([06](../06-EVALUATION.md#baseline-experiment-four-strategies)). It is the first code that spends money in a loop. Three questions needed a structural answer:

1. Where routing decisions live, and how the `arpeggio` strategy's rules are expressed so they work under every profile (`free` has no paid frontier model, `micro` has one provider).
2. How a strategy escalates without leaking hidden tests to the next attempt.
3. How spend is bounded before and during a run, given that the only cost check so far was the per-call spend guard of M0.3.

Agent adapters arrive in v1, so every strategy has to use the single-shot patch executor ([ADR-0008](0008-single-shot-patch-executor.md)).

## Decision

- **A `routing/` package** holds route resolution (`resolve.py`) and the policy file (`policy.py`). Both are pure functions over the config, property-tested. Resolution only ever considers models allowed for the task's repo: the profile (BUD-02), SAF-07 (data use, opt-in, and for `client` repos the `provider_allow` list) and the placeholder guard. No allowed model means a skip with an actionable message, never a fallback.
- **The policy file names tiers and efforts, not model keys** (`tier: 2`, `tier: strongest`). The packaged default lives next to the code. `~/.arpeggio/policy.yaml` replaces it entirely. It is YAML read with `safe_load` ([ADR-0009](0009-pyyaml-for-eval-tasks.md)) and validated by Pydantic with unknown fields rejected. This is RTE-04 and the rule router of [ADR-0003](0003-rule-based-router-first.md), scoped to eval runs for now.
- **Strategies are fixed route sequences**, planned before anything runs (`evals/strategies.py`). That makes `--dry-run` exact about what would run and lets the estimate be an upper bound.
- **Escalation feedback is a compact report** (`evals/feedback.py`): failure kind, failing check numbers and exit codes, and an output tail only where the output cannot show a hidden test. The rule is conservative: output is withheld when the arguments name a hidden path, one of its parents or its module form, or name no path at all, or when the output mentions a hidden file name.
- **Spend is bounded twice.** Before the run, the worst-case estimate (the M0.3 guard formula per planned attempt) must fit within `min(--max-usd, eval_per_month_usd - spent this month)`. During the run, each attempt starts only if spend so far plus its worst case still fits. A stopped run is recorded as `partial` with its reason.
- **`run_patch_attempt` gains hooks** instead of a second executor: a `route_reason`, a `feedback` section, a `base` commit, and a `prepare` callback that runs setup commands and copies hidden tests between applying the patch and running the checks.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Policy rules naming model keys (the v1 sketch in docs/05) | Matches the long-term shape | A separate policy per profile; a key missing under `free` breaks the rule |
| **Rules over tiers and efforts (chosen)** | One policy for every profile; resolution rules are shared with the other strategies | A rule cannot pin one provider's model; that waits for the v1 router |
| Feedback with the full output of failing checks | More signal for the next attempt | Leaks hidden assertions whenever a runner discovers tests by directory |
| Cap that stops only after spend reaches the limit | Simpler, spec-minimal | Can overshoot by a whole attempt; the strict pre-check costs nothing extra |
| A separate eval executor | No change to the patch attempt | Two code paths to judge the same thing; drift between eval and normal runs |

## Consequences

- The `arpeggio` strategy measures the rule table with the task's `expected_risk` as risk. The risk classifier of v1 will need its own baseline.
- The estimate is deliberately pessimistic (two characters per token, full `max_tokens` on every call, every escalation taken). Small budgets will refuse runs that would in practice fit. `[evals] max_tokens` is the knob for that.
- Feedback withholds output for bare test runners, so escalated attempts on such tasks get exit codes only.
- Changes to `routing/` need the eval gate once it exists (AGENTS.md rule 5).

## Revisit when

The v1 router replaces the rule table (bandit, RTE-07), agent adapters arrive (M1.1) and need their own escalation feedback, or batch APIs make a different cost estimate necessary.
