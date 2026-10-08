# ADR-0008: Single-shot patch executor

- **Status:** accepted
- **Date:** 2026-10-08

## Context

The v0 baseline (M0.6) needs models to solve coding tasks, but agent adapters such as Claude Code and opencode only arrive in v1. The one executor Arpeggio has, the `api` adapter from M0.3, returns text and cannot edit files. Without some way to turn a model's answer into a code change, v0 cannot measure anything end to end.

Letting model text change files carries real risk. A reply can name a path outside the repository, write into `.git/`, create a symlink, or carry a huge payload. The done criteria then execute code the model wrote.

## Decision

Add a single-shot patch mode to the `api` adapter (EXE-08). The model is asked for exactly one unified diff against the repository root. Arpeggio extracts it, validates it, applies it with `git apply` in the attempt's own worktree, commits it on the attempt branch, and only then runs the done criteria. There is one model call per attempt, no tools, no loop, and no second try after a bad patch.

A diff is rejected unless every one of these holds:

- The reply contains exactly one fenced block whose info string is `diff` or `patch`.
- The diff is at most 200 KB.
- Every path is relative, has no `..` segment, is not under `.git/`, is not C-quoted, and resolves inside the worktree after following any existing symlinked directories.
- There is no binary patch, no symlink creation, and no mode change to or from a symlink.
- After applying, the files git reports as changed are exactly paths the diff declared.

Every git command Arpeggio runs disables hooks, sets `core.autocrlf=false` and turns off commit signing. Commits use `--no-verify`. Nothing from the user's git setup runs, and the worktree holds files exactly as committed. A rejected or non-applying patch ends the attempt with status `error` and a stored reason (`patch_missing`, `patch_ambiguous`, `patch_unsafe`, `patch_does_not_apply`).

### Why this does not contradict ADR-0001

[ADR-0001](0001-orchestrate-existing-agents.md) chose to orchestrate existing agents rather than build a competing agent loop. Patch mode has no loop. It sends one request, takes one diff and hands the result to verification. It never feeds results back to the model, calls tools or iterates. It is a minimal executor so v0 can measure models on simple tasks, and agent adapters remain the main executors from v1.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Wait for agent adapters before any baseline | No new executor | v0 cannot run end to end, and the baseline slips to v1 |
| Ask the model for whole replacement files | Simple to apply | Large replies, costly, and easy to clobber unrelated content |
| Small built-in agent loop with file tools | Solves more tasks | Directly competes with existing agents, which ADR-0001 rejects |
| **One unified diff, validated, applied with `git apply` (chosen)** | Small replies, git does the applying, every change is a reviewable commit | Only small single-step changes succeed |

## Consequences

- Only small, single-step changes can succeed in patch mode. Larger tasks wait for agent adapters.
- Check commands run code the model wrote, with the user's filesystem permissions. Arpeggio scrubs the environment, so provider keys never reach a check, but it does not isolate the filesystem. That is the container sandbox (EXE-07, v1).
- Every applied patch is a commit on `arpeggio/<task>/<attempt>`, so it can be inspected with ordinary git tools.

## Revisit when

Agent adapters land (M1.1), or M0.6 shows that the diff format itself, rather than the models, limits success.
