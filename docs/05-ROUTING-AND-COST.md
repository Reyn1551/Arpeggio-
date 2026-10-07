# 05 — Routing & Cost

The router answers *"which route is most likely to succeed at the lowest total cost?"*. The cost governor answers *"how much may we spend, and when do we stop or escalate?"*. Both read the same store.

A **route** is `(adapter, model, effort, verification_depth)`. Effort is a routing dimension of its own: a mid-tier model at low effort is often enough for easy work and much cheaper than the same model at high effort.

## Model tiers

Tiers are logical. Concrete models and prices live in config and change over time.

| Tier | Role | Typical use |
|---|---|---|
| `tier1` (cheap) | Worker / utility | Intake classification, summarizing logs and tool output, commit messages, formatting, renames, boilerplate |
| `tier2` (mid) | Default builder | Most features, local refactors, data scripts, tests |
| `tier3` (frontier) | Planner / expert / reviewer | Architecture, hard debugging, cross-file changes, `full` reviews, high-risk tasks |

### Example config (`config/arpeggio.example.toml`)

```toml
[budget]
per_task_usd   = 2.00
per_day_usd    = 10.00
per_month_usd  = 150.00
overhead_alert = 0.10        # alert if Arpeggio's own overhead exceeds 10% of spend

[providers.anthropic]
kind    = "anthropic"
api_key = "env:ANTHROPIC_API_KEY"

[providers.deepseek]
kind     = "openai_compatible"
base_url = "https://api.deepseek.com"
api_key  = "env:DEEPSEEK_API_KEY"

# Prices are placeholders. Fill them from each provider's pricing page and
# record the date; Arpeggio snapshots the price on every call.
[models."tier1.cheap"]
provider = "deepseek"
model    = "<provider-model-id>"
price_in_per_m  = 0.0
price_out_per_m = 0.0
efforts  = ["low", "medium"]

[models."tier2.mid"]
provider = "anthropic"
model    = "<provider-model-id>"
price_in_per_m  = 0.0
price_out_per_m = 0.0
efforts  = ["low", "medium", "high"]

[models."tier3.frontier"]
provider = "anthropic"
model    = "<provider-model-id>"
price_in_per_m  = 0.0
price_out_per_m = 0.0
efforts  = ["medium", "high", "max"]

[defaults]
counterfactual_route = { adapter = "claude_code", model = "tier3.frontier", effort = "high" }
```

## Risk classification

Risk comes from **checkable signals first**, a model estimate second, and the **higher of the two wins**.

| Signal | Weight | Detected by |
|---|---|---|
| Touches paths matching `auth`, `security`, `payment`, `billing`, `permission` | +3 | Planned file list / diff |
| Touches migrations, schema, or data-deleting code | +3 | Path patterns, SQL keywords |
| Touches deploy, CI, infra, or production config | +3 | Path patterns |
| Repo privacy class is `client` | +2 | Repo config |
| Planned or actual diff > 300 lines or > 10 files | +2 | Intake plan / diff stats |
| Requires commands on the approval list | +2 | Intake plan |
| No automated tests cover the touched area | +1 | Coverage map or absence of tests |
| Only docs, comments, formatting, or tests changed | −2 | Diff classification |

Score → level: `≤ 0` = low, `1–3` = medium, `≥ 4` = high. Weights live in config and are tuned only through the eval gate.

Risk is re-evaluated **after** execution using the real diff. If post-hoc risk is higher than routed risk, verification is upgraded to `full` before acceptance.

## Routing policy v1 (rule-based)

Declarative, first match wins (`config/policy.example.yaml`):

```yaml
version: 1
rules:
  - id: high-risk
    when: { risk: high }
    route: { adapter: claude_code, model: tier3.frontier, effort: high, verification: full }
    escalation: none                 # failures go to the user, not to another model

  - id: docs-and-format
    when: { risk: low, category: [docs, format, rename, commit_message] }
    route: { adapter: api, model: tier1.cheap, effort: low, verification: light }
    escalation: [ { model: tier2.mid, effort: medium } ]

  - id: low-risk-code
    when: { risk: low }
    route: { adapter: claude_code, model: tier2.mid, effort: low, verification: light }
    escalation: [ { model: tier2.mid, effort: high }, { model: tier3.frontier, effort: high } ]

  - id: medium-default
    when: { risk: medium }
    route: { adapter: claude_code, model: tier2.mid, effort: medium, verification: light }
    escalation: [ { model: tier3.frontier, effort: high, verification: full } ]

  - id: fallback
    when: {}
    route: { adapter: claude_code, model: tier3.frontier, effort: high, verification: full }
    escalation: none
```

Rules:

- Escalation creates a **new attempt** that receives the previous attempt's failure report (failing checks, error output), never its full transcript.
- Maximum attempts per task: 3 (configurable). After that the task goes to `needs_user`.
- Provider fallback (outage, 429, 5xx) is not escalation: it retries the **same tier** on another allowed provider and does not count as a failed attempt.

## When cascade pays off

Cascading (try cheap first, escalate on failure) only saves money when the cheap route succeeds often enough:

```
expected_cascade = c_cheap + (1 − p) · c_expensive
cascade is cheaper  ⇔  p > c_cheap / c_expensive
```

where `p` is the cheap route's measured success rate for that category, and costs include verification. Cascade also adds latency and risks plausible-but-wrong results slipping through weak verification. Therefore:

- Cascade is allowed only for categories whose done criteria include real tests (`kind = command` with a test runner).
- In v2, the policy auto-disables cascade for a category when `p` (lower confidence bound) falls below the break-even ratio.

## Learned router (v2): contextual bandit

**Context** (features): category, risk level, repo, language, planned diff size bucket, has-tests flag.
**Arms**: allowed `(adapter, model, effort)` combinations for that context (verification depth stays rule-driven by risk).
**Reward**:

```
r = solved − λ · normalized_cost − μ · interventions
```

with `λ`, `μ` in config (start with `λ = 0.3`, `μ = 0.2`).

**Algorithm**: Thompson sampling per context bucket. Success is Beta-Bernoulli per arm; cost uses the arm's running mean (cost is observed reliably, success is the uncertain part). Choose the arm maximizing `sampled_p − λ · cost_norm`.

Guardrails:

- Cold start: a bucket with < 10 observations per arm uses the v1 rule.
- High-risk tasks never explore and always use the v1 high-risk rule.
- **Exploration**: 5–10% of low-risk tasks take a non-greedy arm; logged as `mode = 'explore'`.
- **Shadow evaluation**: 5% of cheap-routed, low-risk tasks are re-run on the counterfactual route in a separate worktree, outside the user's flow (`mode = 'shadow'`), budget-capped per day. If the shadow passes checks the cheap route failed, that is a measured quality loss. Without shadow data, savings claims are unverified.
- The bandit must beat the v1 rules on the **holdout** set before replacing them (EVL-04).

## Tournament mode (v2+)

For tasks the user marks important: run the same task on 2–3 routes in parallel worktrees, verify all, present only the best passing one (ranked by verdict, then diff size, then cost). Costs N× by design; never automatic.

## Cost governor

### Budgets

- Per-task, per-day, per-month limits from config.
- At 80% of any budget: warning in CLI/dashboard.
- At 100%: pause and create an `approvals` row with `action = 'budget_extend'`.

### Loop detection

Pause the attempt when any of these hold:

- The same tool call with identical arguments occurs 3 times.
- 8 consecutive steps produce no change in the worktree diff (configurable).
- Attempt cost exceeds `per_task_usd × 0.6` before any check has passed.

### Context diet

The largest cost in agent loops is re-sent input, not output. Rules:

- Tool outputs above 4 KB enter context as head + tail + a pointer; the full output goes to `artifacts/`.
- Escalated attempts get a compact failure report, not the previous transcript.
- Long histories are summarized by `tier1` before they exceed a configurable token threshold.
- Large tasks run subtasks in fresh sessions with only their own spec and relevant files.

### Prompt caching

Keep prompt prefixes byte-stable and ordered: system prompt → taste → skills → repo context → task. Volatile content (timestamps, random IDs) never appears in the prefix. Record cached tokens where providers report them.

### Batch jobs

Reflection, skill distillation, and eval runs are not urgent. Use provider batch APIs where available (some providers discount batch requests significantly in exchange for delayed results).

### Counterfactual cost

For every finished task, estimate the cost on `defaults.counterfactual_route` using the task's observed token volume scaled by the counterfactual model's prices. Savings = counterfactual − actual. Report it, but treat it as an estimate; shadow evaluation is the ground truth.

### Subscriptions vs API

Subscription plans are usage-limited and tied to each provider's official apps and terms. Arpeggio may drive official CLIs in their supported headless modes, but it must not circumvent limits, and adapters that cannot report exact tokens must mark costs as estimated. Check each provider's current terms before automating on top of a subscription.

## Metrics this layer must report

- Cost per solved task, by category and route.
- Share of tasks escalated, and cost spent on failed attempts.
- Exploration and shadow spend (should stay within configured caps).
- Overhead share: intake + routing + verification model calls as a fraction of total spend.
- Counterfactual savings, with shadow-measured quality loss next to it.
