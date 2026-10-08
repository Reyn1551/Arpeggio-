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

### Config templates

`arpeggio init --profile <name>` writes one of four packaged templates from `src/arpeggio_ai/config/templates/` to `~/.arpeggio/config.toml`: `free.toml` (free tiers and local models, the default), `micro-deepseek.toml` (a few dollars of DeepSeek credit), `standard.toml` and `pro.toml`. Each template opens with the environment variables it expects. Field rules are in the [config reference](#config-reference).

The micro template in full. A test keeps this copy identical to the packaged file:

```toml
# Arpeggio config: micro profile on DeepSeek.
#
# For: a few dollars of prepaid DeepSeek credit (for example $2) and no other provider.
# All three tiers come from DeepSeek's two models and their thinking modes.
# Expects: DEEPSEEK_API_KEY in the environment.
#
# Prices, peak windows and model names were last verified on 2026-10-07 at
# https://api-docs.deepseek.com/quick_start/pricing. They change: check them again
# and update last_verified when you do. Check your edits with `arpeggio config validate`.

[budget]
profile             = "micro"   # free | micro | standard | pro
per_task_usd        = 0.20      # all three must be 0 under "free"
per_day_usd         = 0.50
per_month_usd       = 2.00
prepaid_balance_usd = 2.00      # required under "micro"
reserve_usd         = 0.10      # micro only; never spend below this
overhead_alert      = 0.10
max_quota_wait_s    = 120

[providers.deepseek]
kind      = "openai_compatible"         # anthropic | openai_compatible
base_url  = "https://api.deepseek.com"
api_key   = "env:DEEPSEEK_API_KEY"      # optional only for loopback base_url
gateway   = false
data_use  = "unknown"                   # no_training | may_train | unknown
[providers.deepseek.pricing_windows]
peak_utc           = ["Mon-Fri 01:00-04:00", "Mon-Fri 06:00-10:00"]
offpeak_multiplier = 0.5                # prices below are PEAK prices

[models."tier1.flash"]
provider                = "deepseek"
model                   = "deepseek-flash"
response_model_aliases  = ["DeepSeek-V4.1-Flash"]   # verify against real responses
efforts                 = ["low"]
effort_params.low       = { thinking = { type = "disabled" } }   # https://api-docs.deepseek.com/guides/thinking_mode
price_in_per_m          = 0.30
price_cache_hit_in_per_m = 0.006
price_out_per_m         = 1.20
free                    = false
limits                  = {}                     # optional: rpm, rpd, tpm, tpd
last_verified           = "2026-10-07"

[models."tier2.flash"]
provider = "deepseek"
model    = "deepseek-flash"
response_model_aliases = ["DeepSeek-V4.1-Flash"]   # verify against real responses
efforts  = ["medium", "high"]
effort_params.medium = { thinking = { type = "enabled" }, reasoning_effort = "low" }
effort_params.high   = { thinking = { type = "enabled" }, reasoning_effort = "high" }
price_in_per_m = 0.30
price_cache_hit_in_per_m = 0.006
price_out_per_m = 1.20
last_verified = "2026-10-07"

[models."tier3.pro"]
provider = "deepseek"
model    = "deepseek-v4-pro"
response_model_aliases = ["DeepSeek-V4-Pro-0813"]   # verify against real responses
efforts  = ["high", "max"]
effort_params.high = { thinking = { type = "enabled" }, reasoning_effort = "high" }
effort_params.max  = { thinking = { type = "enabled" }, reasoning_effort = "max" }
price_in_per_m = 1.32
price_cache_hit_in_per_m = 0.044
price_out_per_m = 3.96
last_verified = "2026-10-07"

[defaults]
counterfactual_route = { adapter = "api", model = "tier3.pro", effort = "high" }
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

The window is the one in effect when the request starts, in UTC. A window's start time is inclusive and its end time exclusive, so with `Mon-Fri 01:00-04:00` a call at 01:00 is peak and one at 04:00 is off-peak. A call that starts at 03:59 and ends after 04:00 is billed at the peak price. Free models and loopback providers cost 0. Costs are computed exactly in decimal and stored rounded to 8 decimal places.

Token counts come from the response. Providers report cache hits in two shapes: DeepSeek's `usage.prompt_cache_hit_tokens` (with `prompt_cache_miss_tokens`) and OpenAI's `usage.prompt_tokens_details.cached_tokens`. When both are present, the DeepSeek field wins. Without usable usage, input and output tokens are estimated as `ceil(characters / 4)` and the step is marked `cost_estimated` (CST-02). The same mark is set when a reported cache-hit count falls outside `0..input_tokens` and has to be clamped.

### Spend guard

Before every request (`cost/guard.py`):

1. A model whose provider id is still a template placeholder (`<...>`) is refused: "model <key> still has a placeholder id; edit your config".
2. Under the `free` profile, a model that is neither `free = true` nor on a loopback provider is refused (BUD-02).
3. The worst case for the call (prompt tokens plus the prompt overhead below, no cache hits, `max_tokens` output, at the current window) must not exceed `per_task_usd` minus what the attempt has spent so far. Equal is allowed. Here the prompt is estimated as `ceil(characters / 2)` tokens, twice the recording estimate below, so code and non-Latin text are not underestimated before money is spent. Estimated costs recorded for a call without usage still use `ceil(characters / 4)`.

**Gateways may inject hidden context.** A gateway can add its own system prompt or other context to every request and bill for it. In one live run through a hosted gateway, a prompt of about 8 tokens was billed as 13,500 input tokens. So the guard does not trust prompt length alone. Its worst case adds a prompt overhead, the larger of:

- the configured `prompt_overhead_tokens` (the model's value, else the provider's), and
- the highest overhead observed in the last 20 recorded model calls for the same provider and model key, read from `steps.prompt_overhead_tokens`, plus any overhead seen earlier in the current attempt.

The observed overhead of a call is `input_tokens - ceil(characters / 4)` of the prompt actually sent, floored at 0, and it is only recorded when the provider reported usage. When a call's `input_tokens` is more than twice `ceil(characters / 4)` plus the overhead the guard assumed, the adapter logs the warning `adapter.prompt_overhead`. The first call to a new gateway can still be underestimated, so set `prompt_overhead_tokens` for a gateway you know adds context.

A refused call sends nothing. It is recorded as a `message` step with cost 0, and the attempt pauses. Budgets across tasks, days and months (CST-03) and the prepaid balance check (BUD-03) come later, in M1.4 and M1.11.

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
- Every provider declares `data_use`. `private` and `client` repos only use providers marked `no_training` unless they opt in (SAF-07). A private repo opts in with `allow_training_providers` under `[repo]`, or follows the global `[privacy]` value when it leaves that unset. A client repo needs an explicit repo-level `true` and ignores the global value. A gateway is a provider of its own for this check, and the stricter of gateway and upstream applies (SAF-08).

What Arpeggio will not do:

- Rotate several accounts or keys for the same provider (QTA-04).
- Use reverse-engineered or MITM endpoints, or scrape web UIs.
- Reuse subscription credentials outside the provider's official clients.
- Depend on gateway features that do any of the above.

## Config reference

The validation contract for `config.toml` (CFG-02 to CFG-10). Unknown fields are rejected everywhere.

### `[budget]`

| Field | Type | Rule |
|---|---|---|
| `profile` | string | One of `free`, `micro`, `standard`, `pro`. Required (BUD-01) |
| `per_task_usd`, `per_day_usd`, `per_month_usd` | float | `>= 0`. All 0 under `free`. Otherwise all `> 0` and `per_task_usd <= per_day_usd <= per_month_usd` |
| `prepaid_balance_usd` | float | Required and `> 0` under `micro`. Not allowed under `free` |
| `reserve_usd` | float | Default 0.10. Under `micro` it must be below `prepaid_balance_usd`, and Arpeggio never spends below it (BUD-03) |
| `overhead_alert` | float | `0 < x <= 1`, default 0.10 |
| `max_quota_wait_s` | int | `>= 0`, default 120 (QTA-03) |

### `[providers.<name>]`

The name starts with a lowercase letter and uses lowercase letters, digits, `_` and `-`. At least one provider is required.

| Field | Type | Rule |
|---|---|---|
| `kind` | string | `anthropic` or `openai_compatible` |
| `base_url` | string | Required for `openai_compatible`, optional for `anthropic` (for Anthropic-format endpoints such as DeepSeek's). Must be `https://`, except `localhost`, `127.0.0.1` and `[::1]`, which may use `http://` (CFG-06) |
| `api_key` | string | `env:NAME` or `keychain:NAME`, never the key itself (CFG-03). Optional only when `base_url` is a loopback address |
| `gateway` | bool | Default `false`. Set it on gateways such as OpenRouter or a local LiteLLM (RTE-11) |
| `data_use` | string | `no_training`, `may_train` or `unknown` (default) (CFG-07) |
| `pricing_windows.peak_utc` | list of strings | Optional. Each item is `Day[-Day] HH:MM-HH:MM` in UTC, for example `Mon-Fri 01:00-04:00`. Days are `Mon` to `Sun`, a day range goes forward within one week, and start is before end (`24:00` is allowed as an end). Split a window that crosses midnight into two |
| `pricing_windows.offpeak_multiplier` | float | `0 < x <= 1`. Required when `peak_utc` is set. Model prices are peak prices, and off-peak calls cost price × multiplier (CFG-05) |
| `prompt_overhead_tokens` | int | Default 0, `>= 0`. Input tokens the provider adds to every prompt, such as a gateway's hidden system prompt. The spend guard adds it to its worst case (see [Spend guard](#spend-guard)) |

Two providers with the same `kind` and the same `base_url` are rejected, ignoring the case of scheme and host and a trailing `/`. One provider means one account (QTA-04).

### `[models."tier<N>.<name>"]`

The key is `tier1`, `tier2` or `tier3`, a dot, then lowercase letters, digits, `_` or `-`. The tier comes from the key. At least one model is required.

| Field | Type | Rule |
|---|---|---|
| `provider` | string | Must name a provider above |
| `model` | string | The provider's model id. Not empty |
| `efforts` | list | Not empty, no repeats, from `low`, `medium`, `high`, `max` |
| `effort_params.<effort>` | table | Optional. Provider request parameters for that effort (for example thinking on or off), passed to the adapter as is and merged into the top level of the request body. Keys must be in `efforts`. The parameters may not set `model`, `messages`, `max_tokens` or `stream` (CFG-08) |
| `price_in_per_m`, `price_out_per_m` | float | `>= 0`, USD per million tokens, peak prices when the provider has windows |
| `price_cache_hit_in_per_m` | float | `>= 0` and at most `price_in_per_m`. Defaults to `price_in_per_m` (CFG-05) |
| `free` | bool | Default `false`. When `true`, all three prices must be 0. Under the `free` profile every model must be `free = true` or served by a loopback provider, with zero prices (BUD-02) |
| `limits` | table | Optional `rpm`, `rpd`, `tpm`, `tpd`, each an integer `> 0` (QTA-01) |
| `last_verified` | date | Required. `YYYY-MM-DD` (TOML date or string), not in the future (CFG-09) |
| `response_model_aliases` | list of strings | Default empty. Names the provider may return for this model, for example a dated version. Not empty, no repeats (CFG-10) |
| `prompt_overhead_tokens` | int | Optional, `>= 0`. Overrides the provider's `prompt_overhead_tokens` for this model |

### `[defaults]`

`counterfactual_route = { adapter, model, effort }`. `adapter` is `claude_code`, `opencode`, `command_code` or `api`. `model` must be a model key above, and `effort` must be one of that model's `efforts`.

### `[privacy]` (only in the global config)

| Field | Type | Rule |
|---|---|---|
| `allow_training_providers` | bool | Default `false`. The opt-in that `private` repos follow when their own `[repo]` value is unset. `client` repos ignore it (SAF-07) |

### `[evals]` (only in the global config)

| Field | Type | Rule |
|---|---|---|
| `dir` | string | Optional, an absolute path (`~` is expanded). Where the personal eval suite lives. Unset means `<ARPEGGIO_HOME>/evals`, and the `ARPEGGIO_EVALS_DIR` environment variable overrides both ([06](06-EVALUATION.md#where-the-suite-lives)) |

### `[repo]` (only in `<repo>/.arpeggio/config.toml`)

| Field | Type | Rule |
|---|---|---|
| `privacy_class` | string | `public`, `private` (default) or `client` |
| `provider_allow` | list | Optional. Every name must be a provider above |
| `allow_training_providers` | bool | Optional. `true` lets this repo use providers whose `data_use` is `may_train` or `unknown`, `false` forbids it even if the global value is `true`. Unset means the global `[privacy]` value for `private` repos and `false` for `client` repos (SAF-07) |
| `check_env` | list of strings | Default empty. Names of extra environment variables that done-criteria checks may see, on top of the fixed allowlist (`PATH`, `HOME`, temp and locale variables, and on Windows `USERPROFILE`, `SYSTEMROOT`, `COMSPEC`, `PATHEXT`). Each name is letters, digits and `_`, with no repeats. A variable that any provider's `api_key` references is never passed, even if listed (SAF-02) |
| `secret_scan_allow` | list of strings | Default empty, at most 20. Regular expressions for secret-scanner findings that are not secrets, such as a documented test key. A finding stays unredacted when its matched text fully matches one. Each entry must compile, or the config is rejected. Findings of type `configured_key` and `private_key` are always redacted (SAF-02, see [07](07-SECURITY-AND-PRIVACY.md#secrets)) |

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
