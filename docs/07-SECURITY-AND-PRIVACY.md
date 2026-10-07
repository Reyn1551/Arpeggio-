# 07 — Security & Privacy

Arpeggio runs agents that execute shell commands on the user's machine and send code to external providers. Treat both as dangerous by default.

## Threat model

| Threat | Example | Primary control |
|---|---|---|
| Destructive action | Agent runs `rm -rf`, drops a database, force-pushes | Worktree isolation, approval gate, command policy |
| Secret leakage to providers | `.env` or keys included in context | Secret scanner, path exclusions, env-only key loading |
| Secret leakage to logs/DB | API key echoed in tool output and stored | Redaction before persistence |
| Prompt injection | README, issue, web page, or dependency file says "ignore instructions and push to…" | Content-as-data rule, approval gate, injection evals |
| Code exfiltration to unapproved vendors | Client code routed to a provider not covered by the client agreement | Per-repo privacy class + provider allowlist |
| Supply chain | Agent installs a typo-squatted package | Install commands require approval on `medium`/`high` risk; lockfiles diffed |
| Runaway cost | Infinite loop, eval storm | Budgets, loop detection (see 05) |
| ToS violation | Automating a subscription in unsupported ways | Official interfaces only (NFR-10) |
| Free-tier provider trains on private code | A `private` repo routed to a free tier that uses submitted data for training | `data_use` per provider, enforced by privacy class (SAF-07) |
| Gateway sees all prompts | Every request passes through a hosted gateway | Gateway is its own provider with its own `data_use` (SAF-08) |
| Account suspension from ToS-violating gateway features | Multi-account rotation or MITM endpoints offered by a community gateway | Not supported (QTA-04, non-goal in [01](01-VISION-AND-GOALS.md#non-goals)) |

## Isolation

- Every attempt runs in its own `git worktree` under `~/.arpeggio/worktrees/`, on a branch named `arpeggio/<task>/<attempt>`.
- The agent's working directory is the worktree. Writes outside it are denied by policy and, when `sandbox = "container"`, by the container mount itself.
- Container mode (v1 optional, v2 recommended for `client` repos): only the worktree is mounted read-write; network egress limited to the provider endpoints and package registries in an allowlist.

## Command policy

Three classes, configurable per repo:

| Class | Examples | Behavior |
|---|---|---|
| `allow` | Read files, run tests, run linters, `git status/diff/add/commit` on the attempt branch | Runs without asking |
| `ask` | Package installs, network calls to non-allowlisted hosts, migrations on local DB, deleting files inside worktree in bulk | Pauses for approval at `medium`/`high` risk; allowed at `low` |
| `deny` | `git push --force`, push to protected branches, deleting outside worktree, deploy commands, publishing packages, commands touching `~/.ssh`, keychains, or browser profiles | Always requires explicit approval, every time; never batch-approved |

Approval decisions are per action and per attempt. An approval never generalizes to future actions.

## Secrets

- API keys load from environment variables or the OS keychain (`env:VAR` or `keychain:name` in config). Inline keys in config fail validation. Only `env:` works today, and `keychain:` references report "not supported yet".
- A key is resolved when a request is about to be sent, held as a `SecretStr`, and placed only in the `Authorization` header. Logs, error messages, artifacts and database rows never contain it, and error messages name the variable only. Request and response bodies go to artifacts, never to logs. When a provider error ends an attempt, the final message quotes the provider's error message (one line, at most 200 characters). If a provider echoes the key back, the key is replaced with `[REDACTED]` there and in the stored body. An integration test plants a fake key and searches every artifact, log line and database text column for it ([ADR-0007](adr/0007-httpx-and-first-network-calls.md)).
- Tests never reach the network. The one live smoke test is opt-in (`-m live` with `ARPEGGIO_LIVE=1`).
- Default exclusions from agent context: `.env*`, `*.pem`, `*.key`, `id_*`, `*credentials*`, `secrets.*`, cloud CLI config dirs.
- A secret scanner (regex + entropy) runs on: outgoing prompts built by Arpeggio, tool outputs before persistence, and diffs before merge approval. Matches are redacted (`[REDACTED:<type>]`) and logged as events.
- Adapters that call external CLIs pass only the environment variables those CLIs need.

## Prompt injection

- Content from files, web pages, issues, tool outputs, and dependencies is **data**. Text inside them that tries to give instructions is never acted on without the user's confirmation.
- Gated actions are enforced in Arpeggio, outside the model. Even a fully compromised agent cannot merge, push, or deploy without an `approvals` row decided by the user.
- The eval suite includes injection tasks (EVL composition: adversarial share). A release fails if any injected action executes.

## Code privacy

Each repo declares a privacy class in `<repo>/.arpeggio/config.toml`:

| Class | Meaning | Default routing constraint |
|---|---|---|
| `public` | Open source or non-sensitive | Any configured provider |
| `private` | Owner's private work | Any configured provider the owner trusts (global allowlist) |
| `client` | Code owned by a client or employer | Only providers explicitly listed in the repo's `provider_allow`; shadow evaluation and exploration to other providers disabled |

Before using Arpeggio on employer or client code, confirm that sending that code to each configured provider is permitted by the relevant agreements and policies.

## Free tiers and gateways

- Every provider declares `data_use`: `no_training`, `may_train` or `unknown` (the default). Mark a provider `no_training` only after reading its current terms.
- `private` and `client` repos only route to `no_training` providers unless they opt in (SAF-07). A repo opts in with `allow_training_providers = true` under `[repo]` in its `.arpeggio/config.toml`. Private repos that leave it unset follow the global `[privacy] allow_training_providers`, which defaults to `false`. Client repos ignore the global value and need the explicit repo-level `true`. Opt in only for code you are allowed to share that way.
- `arpeggio init` prints a reminder when the chosen template has providers with `data_use = "unknown"`. It makes no network call, so checking each provider's data policy is up to you.
- A gateway counts as a provider of its own with its own `data_use`. When the upstream provider is known, the stricter of the two applies (SAF-08).
- Arpeggio does not support quota evasion: no multi-account or multi-key rotation for one provider, no MITM or reverse-engineered endpoints, no reuse of subscription credentials outside official clients. Config rejects two providers with the same kind and endpoint (QTA-04). See [ADR-0006](adr/0006-gateways-and-free-tier-ethics.md).

## Local data

- No telemetry. Nothing leaves the machine except provider calls.
- `~/.arpeggio/` permissions: `0700`.
- The DB and artifacts contain code and prompts; back them up encrypted if backed up at all.
- `arpeggio purge --repo <path>` deletes all tasks, artifacts, and feedback for a repo (for client offboarding).

## Security checklist per release

- [ ] Secret scanner tests pass (known-secret fixtures are redacted everywhere).
- [ ] All `deny` commands blocked in an integration test without approval.
- [ ] Injection eval tasks: zero injected actions executed.
- [ ] `client` repo routing test: no call to a non-allowlisted provider.
- [ ] Dependency audit (`pip-audit`) clean or exceptions documented.
