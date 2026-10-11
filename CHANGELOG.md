# Changelog

All notable changes to Arpeggio are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Arpeggio is **pre-1.0**:
while the version is `0.x`, a minor bump may contain a breaking change, and those are called out
under **Changed** or **Removed**.

Requirement IDs (`CFG-01`, `EXE-08`, `EVL-05`, …) refer to
[docs/02-REQUIREMENTS.md](docs/02-REQUIREMENTS.md), and milestone names to
[docs/08-ROADMAP.md](docs/08-ROADMAP.md).

## [Unreleased]

Nothing yet.

## [0.0.1] — 2026-10-10

Everything below is the v0 line: "a store, one adapter, and a real baseline. Nothing smart yet."

### Added — M0.1 Skeleton

- `arpeggio init` with a validated config schema, global and per-repo config deep-merged
  (CFG-01..04, CLI-01).
- CLI entry point with `--version`, `config validate`, `config show` and `--json` output
  (CLI-01, CLI-03).
- ULID ids and ISO-8601 UTC timestamps; structured JSON-lines logging carrying task and attempt
  context (OBS-03).

### Added — M0.2 Store

- SQLite schema v1, a migration runner that backs up the database before migrating, and
  repositories for repos, tasks, attempts and steps (OBS-01).
- Artifact store under `ARPEGGIO_HOME/artifacts` (OBS-01).

### Added — M0.2.5 Budget profiles

- `budget.profile` with per-profile rules, model pricing, limits, effort parameters and response
  aliases, plus four packaged templates and `arpeggio init --profile` (CFG-05..10, BUD-01,
  QTA-04, CLI-05, NFR-12).
- Provider gateway, `data_use`, pricing windows and loopback rules (CFG-05..07).
- Migration 0002 for budget profiles; migration 0003 renames `tasks.deferrable` to
  `is_deferrable` (CST-10).

### Added — M0.2.6 Privacy defaults

- Global `[privacy]` opt-in with repo and client precedence, and a reminder in `init` about
  providers whose `data_use` is unknown (SAF-07).
- Behavioral budget defaults stated in every template (NFR-12).

### Added — M0.3 First adapter

- Adapter protocol and registry (EXE-01).
- OpenAI-compatible `api` adapter with retries and backoff (EXE-02, RTE-11, RTE-12).
- Pricing engine with cache-hit pricing and price windows, and a spend guard covering free
  profiles, placeholders and the per-call worst case (CST-11, CST-02, BUD-02).
- `env:` secret references resolved without exposing the value (NFR-06).
- Steps, artifacts and model mismatch recorded per attempt (CST-01, RTE-11).

### Added — M0.4 Worktrees and verifier

- Worktree per attempt at an explicit base commit, with a custom branch (EXE-04).
- Done-criteria runner in the worktree, executing checks with a scrubbed environment, a timeout
  and a process-tree kill; on Windows, a Job Object kills the whole tree (EXE-06, SAF-02,
  VER-01, VER-04).
- Check output bounded to the last 10 MB, killed past 100 MB, temp file always deleted (EXE-06).
- Single-shot patch mode: extract, validate and apply one unified diff, then run the checks
  (EXE-08, [ADR-0008](docs/adr/0008-single-shot-patch-executor.md)).

### Added — M0.4.1 Hardening

- Content secret scanner with redaction, allowlist and chunking; secrets redacted in text
  artifacts, log records and outbound messages, and context scanned per file before truncation
  (SAF-02, NFR-06).
- Patch line endings normalized per file, raw and applied diffs stored separately, and a patch
  whose added lines contain a redaction placeholder is refused (EXE-08, SAF-02).
- `arpeggio secrets scan` as a dry run reporting file, line and type only (SAF-02).

### Added — M0.5 Eval suite

- Eval task format and validator across tuning and holdout splits; personal suite located outside
  the repository via `[evals] dir` or `ARPEGGIO_EVALS_DIR` (EVL-01, EVL-02,
  [ADR-0009](docs/adr/0009-pyyaml-for-eval-tasks.md)).
- `eval new` scaffolds a task from two commits and self-checks it at base and solution,
  including a generated text-check hidden test; `eval check` and `eval list` (EVL-01).
- Hidden test paths of an attached eval task are refused as prompt context (SAF-02).
- Public sample suite under `evals/sample/`.

### Added — M0.6 Baseline

- `eval run` over the `junior`, `middle`, `senior` and `arpeggio` strategies on the patch
  executor, with escalation carrying a failure report, and a safe `npm`/`npx` path on Windows
  (EVL-03, EVL-06, RTE-04, EXE-09).
- Rule policy file over tiers ([ADR-0010](docs/adr/0010-baseline-runner-and-policy-file.md)),
  worst-case cost estimate, `eval_per_month_usd` and a run cap, and a peak-price warning
  (EVL-03, CST-05).
- `eval report` with bootstrap confidence intervals, paired differences against `middle`,
  per-profile and per-split sections, tag breakdowns, cost per solved task, and Markdown output
  kept outside the checkout (EVL-05).
- `eval runs`, `--config` for per-profile runs, and migration 0006 (EVL-03, EVL-06).

### Added — M0.6.1 Truncation

- A truncated reply (`finish_reason` other than `stop`) fails the attempt as `output_truncated`
  and escalates, instead of being applied as a partial diff (EXE-08, VER-03).
- `finish_reason` and reasoning tokens recorded on model-call steps, and the report counts
  attempt outcomes and the reasoning share (CST-01, EVL-05).

### Added — M0.7 Patch format

- Hunk headers recounted before apply, so a diff with wrong counts is not rejected blindly
  (EXE-08).
- `patch_unknown_path` and `patch_malformed`: paths must exist at the base, and inconsistent
  `a/` `b/` headers are refused (EXE-08).
- Patch paths grounded in the context actually provided to the model (EXE-08).
- Report counts `patch_unknown_path`, `patch_malformed` and context redactions (EVL-05, SAF-02).

### Added — M0.8 Edit-format experiment

- Two experimental edit formats behind `eval run --edit-format`, each with its own pinned system
  prompt: `search-replace` (match-once blocks, `patch_search_not_found`,
  `patch_search_ambiguous`, `patch_edit_overlap`) and `whole-file`
  (`patch_mixed_line_endings`) (EXE-08).
- `eval matrix RUN_ID ...` groups runs by format, rejects runs that differ in strategy, split,
  profile, `config_hash` or `git_sha`, and applies the acceptance rule fixed before the run
  (EVL-05).
- `eval runs` filters by `--strategy`, `--git-sha`, `--config-hash` and `--since`; `eval run
  --round` stores the experiment round (EVL-03, EVL-05).

### Changed

- **`udiff` remains the only format outside the experiment.** `arpeggio run` and every other
  path are unchanged; `--edit-format` is experimental and reaches only `eval run` (EXE-08).

### Notes on M0.8

M0.8 measured whether another way of writing the change solves more tasks. It does not. Three
runs per format on the tuning split with `middle` at $0 gave a mean solved of **1.33** for
`udiff`, **1.67** for `whole-file` and **1.00** for `search-replace`. Neither candidate reached
the +2 required, and both lost `carboncompare-tree-no-carbon`, which the baseline solved. The
milestone therefore closed with **"the format is not the binding constraint"**: EXE-08 and
ADR-0008 stand, no holdout artifact was opened, and the next work targets prompt design and
context selection.

### Verified

- 1420 tests pass, 4 skipped (POSIX-only, on Windows), `ruff check`, `ruff format --check` and
  `mypy --strict` clean.

[Unreleased]: https://github.com/Reyn1551/Arpeggio-/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/Reyn1551/Arpeggio-/releases/tag/v0.0.1
