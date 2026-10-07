# 05 — Routing & Cost

The router answers *"which route is most likely to succeed at the lowest total cost?"*. The cost governor answers *"how much may we spend, and when do we stop or escalate?"*. Both read the same store.

A **route** is `(adapter, model, effort, verification_depth)`. Effort is a routing dimension of its own: a mid-tier model at low effort is often enough for easy work and much cheaper than the same model at high effort.

## Budget profiles

Arpeggio runs the same pipeline at every budget. The profile (`budget.profile` in config, stored on every task) decides which routes exist, how high quality can go, and how fast work gets done. It never switches a feature off without a $0 alternative (BUD-04, [ADR-0005](adr/0005-budget-profiles.md)).

| Profile | Typical user | Allowed routes | Verification `full` means | Shadow / tournament |
|---|---|---|---|---|
| `free` | $0 | Free-tier and local models only | Full test suite + all static checks | Disabled |
| `micro` | ≤ ~$5 prepaid on one provider | Cheapest provider models, free tiers first for low risk | Full suite + review by the strongest configured model | Shadow ≤ 2% of tasks, capped by balance |
| `standard` | Regular paid usage | All configured | As currently specified | As currently specified |
| `pro` | Multiple paid providers / subscriptions via official clients | All configured | As currently specified | Enabled |

### Feature degradation matrix

Every feature works under every profile. Where a feature costs money, the `free` column names its $0 version.

| Feature | `free` | `micro` | `standard` | `pro` |
|---|---|---|---|---|
| Intake | Strongest free model, within its quota | Cheapest model, thinking off | `tier1` | `tier1` |
| Routing | Rules over free and local models only, never a paid route (BUD-02, QTA-03) | Rules, free tiers first for low risk, every call checked against the remaining balance (BUD-03) | Rules in v1, bandit in v2 | Same as `standard` |
| Cascade | Between free models only | Within one provider (for example flash to pro), only above break-even | As specified (CST-09) | As specified |
| Verification | `full` = full suite + all static checks, no model review (VER-02) | `full` = full suite + review by the strongest configured model | As specified (VER-02) | As specified |
| Reflection | Failure classes from verdicts and logs, plus a free model when quota is left, deferred | Cheapest model, deferred to the cheapest price window | Batch API where available | Same as `standard` |
| Taste | Distilled from diffs by a free model, deferred | Cheapest model, deferred | As specified | As specified |
| Shadow evaluation | Disabled | ≤ 2% of tasks, capped by balance | As specified | Enabled |
| Tournament | Disabled | Disabled | Manual only, never automatic | Manual, enabled |
| Eval runs | Free models, deferred, quota waits reported | Deferred to off-peak, capped by balance | As specified | As specified |

"As specified" means the behavior described in the rest of this document.

## Model tiers

Tiers are logical. Concrete models and prices live in config and change over time.

| Tier | Role | Typical use |
|---|---|---|
| `tier1` (cheap) | Worker / utility | Intake classification, summarizing logs and tool output, commit messages, formatting, renames, boilerplate |
| `tier2` (mid) | Default builder | Most features, local refactors, data scripts, tests |
| `tier3` (frontier) | Planner / expert / reviewer | Architecture, hard debugging, cross-file changes, `full` reviews, high-risk tasks |

### Example config (`src/arpeggio_ai/config/templates/config.example.toml`)

`arpeggio init` copies the packaged template to `~/.arpeggio/config.toml`. The template carries more comments than the excerpt below, and if the two ever differ, the template is correct.

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

## Pricing model

Prices are data, not code (NFR-12). Each model in config declares three prices per million tokens: input (cache miss), cache-hit input, and output. A provider may also declare peak windows in UTC with an off-peak multiplier. Prices in config are always the **peak** prices. Providers without windows have flat pricing (multiplier 1).

Cost of one call (CST-11):

```
cost = ( (input_tokens − cached_tokens) × price_in_per_m
       + cached_tokens × price_cache_hit_in_per_m
       + output_tokens × price_out_per_m ) / 1,000,000 × multiplier
```

`cached_tokens` counts the input tokens the provider served from its cache. `multiplier` is `offpeak_multiplier` when the call starts outside every peak window, and 1 otherwise. Each step records the prices it used, the window (`peak`, `offpeak` or `flat`) and the multiplier, so history never depends on today's prices.

Deferrable work (CST-10: eval runs, reflection, distillation, shadow evaluation) is scheduled into the cheapest upcoming window when the provider declares peak windows.

## Quota governor

Free tiers come with per-minute and per-day caps that differ per model and change without notice.

- Models may declare `limits` (`rpm`, `rpd`, `tpm`, `tpd`). Arpeggio counts usage locally per minute and per day (QTA-01).
- When a response carries rate-limit headers, the headers win over the configured values (QTA-02).
- On HTTP 429 or an exhausted quota (QTA-03), the router waits if the reset is at most `max_quota_wait_s` away (default 120 s). Otherwise it switches to another allowed route of equal or lower cost, or pauses the task. Under `free` it never switches to a paid route.
- One provider is one account. Config rejects two providers with the same kind and endpoint (QTA-04).

## Single-provider mode (DeepSeek example)

A `micro` user often has credit on one provider only. Single-provider operation is fully supported (RTE-12): provider fallback becomes retry with backoff, then pause. The three tiers come from one provider's models and thinking modes:

| Tier | Route |
|---|---|
| `tier1.flash` | `deepseek-flash`, thinking off |
| `tier2.flash` | `deepseek-flash`, thinking on |
| `tier3.pro` | `deepseek-v4-pro`, thinking on |

Prices last verified 2026-10-07 on the [DeepSeek pricing page](https://api-docs.deepseek.com/quick_start/pricing). They change, so treat them as data to re-check, not as constants. Per million tokens at peak: flash costs $0.30 input, $0.006 cache-hit input and $1.20 output. Pro costs $1.32, $0.044 and $3.96.

**Cascade break-even.** Pro costs 3.3× (output) to 4.4× (input) as much as flash. Using `p > c_cheap / c_expensive` from [When cascade pays off](#when-cascade-pays-off), trying flash first pays off once flash's measured success rate for a category is above roughly 23-30%. Thinking mode changes output volume a lot, so measure the real ratio per category instead of trusting the price ratio.

**Off-peak deferral.** Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday, which is 08:00-11:00 and 13:00-17:00 in UTC+7. Off-peak calls cost 50% of peak. DeepSeek excludes Chinese public holidays from peak, but Arpeggio treats them as peak, which errs on the expensive side. Deferrable work waits for the next off-peak window.

## Gateways and free tiers

Gateways (OpenRouter, 9Router, LiteLLM and similar) and local servers are configured as ordinary `openai_compatible` providers ([ADR-0006](adr/0006-gateways-and-free-tier-ethics.md)).

- Set `gateway = true` on gateways. Every call records the model that actually answered. A mismatch with the requested model, after `response_model_aliases`, is flagged, and that attempt is kept out of router learning (RTE-11, CFG-10).
- Turn off gateway-side fallback, or pin the model. Arpeggio never relies on it for learning data.
- Every provider declares `data_use`. `private` and `client` repos only use providers marked `no_training`, unless the repo sets `allow_training_providers = true` (SAF-07). A gateway is a provider of its own for this check, and the stricter of gateway and upstream applies (SAF-08).

What Arpeggio will not do:

- Rotate several accounts or keys for the same provider (QTA-04).
- Use reverse-engineered or MITM endpoints, or scrape web UIs.
- Reuse subscription credentials outside the provider's official clients.
- Depend on gateway features that do any of the above.

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
