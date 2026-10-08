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

### What patch mode does not isolate

In single-shot patch mode ([ADR-0008](adr/0008-single-shot-patch-executor.md)) the done criteria execute code the model wrote, with your user's filesystem permissions. Arpeggio checks the diff before applying it (paths, `.git`, symlinks, binary data, size) and scrubs the environment of checks, but it does not restrict what a test file does on disk or on the network. Run patch mode only on repositories and tasks where that is acceptable until the container sandbox (EXE-07) exists.

On timeout a check's process tree is killed. On POSIX this is the process group, so only a child that starts its own session escapes. On Windows every check runs inside its own Job Object, so the whole tree dies on timeout or output limit, including a grandchild whose parent already exited. After a normal exit, anything the check left running in the job is killed too and logged as `process.stragglers_killed`. A process started through a Windows service, WMI or Task Scheduler is not a descendant and is not in the job. If Windows refuses to create or assign a job, Arpeggio logs `process.job_object_unavailable` once and falls back to `taskkill /T /F`, which misses an orphaned grandchild ([ADR-0008 amendment](adr/0008-single-shot-patch-executor.md#amendment-2026-10-08-windows-process-tree-kill)).

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
- A content secret scanner (`safety/secret_scan.py`) replaces each finding with `[REDACTED:<type>]` at three chokepoints (SAF-02, NFR-06):
  1. Outgoing model requests. Every message is scanned before the spend guard estimates its size, so the request, the guard and the stored request artifact all see the redacted text. In patch mode the task request and each context file are also scanned on their own, so events name the file, and the attempt's `prompt` step says how many items were redacted.
  2. Artifact writes. Every text artifact is scanned before it is written: request and response bodies, check logs, diffs and failure details. Data counts as text unless its first 8 KiB hold a NUL byte, and text that is not valid UTF-8 is scanned with its other bytes kept as they are.
  3. Log records. Every string value is scanned, on top of the name-based `[REDACTED]` for fields such as `api_key`.

  Text that ends up in a database row (step summaries, failure summaries, the 2,000-character output tail of a verdict) is redacted before it is cut, so a cut never keeps part of a secret. Each chokepoint logs `secret_scan.redacted` with counts per type and a source label such as `context:src/settings.py` or `artifact:check-1.log`. Values never appear in events, and neither do hashes of them.
- Detectors, strongest first: `configured_key` (the value of every `env:` variable a provider's `api_key` names, when set and at least 8 characters long), `private_key` (a `BEGIN ... PRIVATE KEY` block, or everything after a BEGIN line that has no END), the token formats `aws_access_key`, `github_token`, `slack_token`, `google_api_key`, `openai_style_key`, `groq_key` and `jwt`, and `assigned_secret`. Token patterns do not start inside a longer word, so the `sk-` in a kebab-case name such as `risk-assessment` never starts a key. `assigned_secret` is a value of 16 or more characters with at least 3.5 bits of entropy per character, assigned (`=`, `:`, `:=`, `=>`) to a name containing `api_key`, `secret`, `token`, `password`, `access_key` or `client_secret`. It skips placeholders (`<...>`, `${...}`, `{{...}}`, `env:`, `keychain:`, one repeated character, runs of `x` or `*`) and unquoted code: `$var`, `@var`, calls (`(`, `->`, `::`) and dotted or indexed names such as `settings.SECRET_KEY` or `os.environ[...]`. A quoted literal is always checked. A single bare word is checked too, because that is how secrets sit in `.env`, YAML and INI files.
- Findings that are not secrets can be allowed per repo with `[repo] secret_scan_allow` ([config reference](05-ROUTING-AND-COST.md#repo-only-in-repoarpeggioconfigtoml)). `configured_key` and `private_key` findings are always redacted.
- Measure false positives before you rely on redaction in patch mode: `arpeggio secrets scan PATH [--include GLOB ...] [--exclude GLOB ...] [--json]` is a dry run of the same scanner over files. Inside a git repository it reads only files git does not ignore (`git ls-files --cached --others --exclude-standard`), elsewhere every file outside `.git`. Binary files and files over 5 MB are skipped. It uses the repo's `secret_scan_allow` and, when a global config exists, the configured provider keys. The report gives totals per type, the 20 files with the most findings, and per finding only file, line and type. It never shows the matched text or a hash of it.
- Scanning is linear in the input. Texts over 5 MiB are scanned in 5 MiB windows that overlap by 1 KiB, and configured keys and private keys are searched in the whole text.
- Patch mode and redaction: a context file with findings is still sent, with the findings redacted. If the model's diff has context or removed lines on a redacted line, `patch.touches_redacted_lines` is logged and the diff will usually not apply (`patch_does_not_apply`), because the file on disk still holds the real value. Arpeggio accepts that failure, since the alternative is sending the real value. Keep secrets out of files the model has to edit. A diff whose added lines contain `[REDACTED:` is rejected before it is applied (`patch_writes_redacted_placeholder`), so a placeholder never replaces a real value on disk.
- Diffs are not yet scanned before merge approval, because there is no merge step yet.
- Adapters that call external CLIs pass only the environment variables those CLIs need.
- Done-criteria checks and Arpeggio's git commands start from an allowlist, never from your full environment: `PATH`, `HOME`, `USERPROFILE`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LC_ALL`, and on Windows `SYSTEMROOT`, `COMSPEC` and `PATHEXT`, plus the names in `[repo] check_env`. Every variable that a provider `api_key` references is removed, even when `check_env` lists it (SAF-02).

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

## Eval suite privacy

- The personal eval suite holds requests, hidden tests and reference diffs from private repositories. It lives outside the Arpeggio repo (`ARPEGGIO_EVALS_DIR`, `[evals] dir` or `~/.arpeggio/evals/`), ideally as its own private git repository ([06](06-EVALUATION.md#where-the-suite-lives)). Only the synthetic sample suite in `evals/sample/` is public.
- `arpeggio eval new` passes every file it writes through the secret scanner and prints counts per type, never values. Review a task before committing it if the counts are not zero.
- Hidden tests never reach a model. With an eval task attached, the prompt builder refuses a context file whose path is a hidden test path before reading it. `notes` are never put in a prompt.
- `eval check` runs setup commands and checks with the same environment allowlist, timeouts, output caps, process-tree kill and redaction as attempt checks. Its report stores statuses and artifact references, never output.
- `eval run` sends the owner's code to providers, so SAF-07 applies to every route it plans: a `private` repo uses only `no_training` providers unless it opts in, and a `client` repo only providers in its `provider_allow`. A task with no allowed model is skipped with `no_allowed_route` and the opt-in it would need. Opting in is a deliberate act in the repo's own config ([route resolution](05-ROUTING-AND-COST.md#route-resolution-for-eval-runs)).
- An escalated attempt gets a failure report, never a hidden test. Check output is included only when it cannot show one ([the failure report](06-EVALUATION.md#the-failure-report)).
- Baseline reports describe private tasks: `eval report` writes Markdown under the evals directory and refuses a path inside the Arpeggio checkout.

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
