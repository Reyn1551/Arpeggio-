# ADR-0011: Context selection for patch prompts

- **Status:** accepted
- **Date:** 2026-10-11

## Context

Patch mode ([ADR-0008](0008-single-shot-patch-executor.md)) asks the model for one unified diff. To write a diff, the model must be shown the files it is allowed to change: `read_context` reads every path in `EvalTask.context_files` and puts them in the prompt under "Files you may modify, with these exact paths".

That list is hand-written per task. It has to be complete, because the executor rejects any diff naming a path that is not in the base commit, and a task author who forgets a file gets `patch_unknown_path` no matter how good the model is. This is a harness limitation presenting itself as a model failure — the same shape M0.8 found in edit formats, where the binding constraint turned out not to be the model.

M0.8 measured edit formats and returned a **negative** result: mean solved was `udiff` 1.33, `whole-file` 1.67, `search-replace` 1.00, and neither candidate reached the pre-registered +2. A lesson from it is that the eval suite is curated by the same person who writes the tasks, so any hand-tuned input is already close to optimal. Automatic context selection has to be judged with that in mind.

## Decision

Add a deterministic, rule-based context selector (`orchestrator.context`) and let a patch attempt fill its prompt from it.

`rank(request, candidates, *, limit_bytes, max_files)` scores each tracked file by name overlap with the request and returns the highest-scoring paths that fit, with no model call, no network and no randomness. Weights favour a basename named exactly in the request (100), then a basename mentioned (40), a stem (25), a directory (12), a path (8) and an uncommon extension (4), and penalise depth, vendor trees and test files. A request naming nothing selects nothing, rather than guessing.

The selector runs over `git ls-files` in the attempt's worktree, so candidates are exactly the files that exist at the base commit. Caps are shared with the prompt path: `CONTEXT_CAP_BYTES` (100 KB) and `CONTEXT_MAX_FILES` (8). Paths `read_context` always refuses (secret file patterns such as `.env*` or `secrets.*`, anything under `.git`, and otherwise unusable paths such as quoted, absolute, or `..`-escaping ones) are not candidates, so they never take a slot or count against the byte cap, at planning time or in the worktree.

**Selection is not authorisation.** The selector only decides what is *shown*. A file it did not choose is not thereby forbidden: `patch_unknown_path` continues to mean "absent from the base commit", and the worktree, the secret scanner, the hidden-test guard and the approval gate are unchanged. This is the central safety property of the change and is covered by a regression test.

The eval runner learns the same rule through `evals.budget.selected_context_paths`, which ranks `git ls-tree` blobs at `base` — no worktree exists at planning time — so a run's worst-case cost estimate describes the prompt the attempt will really send. Parity between the two paths is itself a test. The `manual` estimate applies the same refusal rule to hand-written `context_files`, so entries the attempt would never read no longer inflate the worst-case cost.

The flag is opt-in. `arpeggio eval run --context-selection auto` selects; the default stays `manual`. `arpeggio run` stays on `manual` until the experiment accepts `auto`. Acceptance is pre-registered: with N=3 rounds on the tuning split, `auto` must beat `manual` by **at least +2** mean solved, and no task solved under `manual` may be unsolved in more than one `auto` run.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Keep hand-written `context_files` only | No new code; the author knows the answer | A forgotten file is indistinguishable from a model failure |
| Embed the whole base commit in the prompt | Never misses a file | Blows the token budget and the cost cap; defeats the point of a small prompt |
| Ask a model to choose the files | Handles vague requests | A second network call per attempt, non-deterministic, and it needs its own eval |
| `git grep` for words from the request | Simple and fast | Misses files that never mention the changed symbol; no useful ordering |
| **Deterministic keyword ranking with weights and caps (chosen)** | No model call, testable and reproducible, degrades to nothing when unsure | Shallow lexical matching, and it will not follow imports or call graphs |

## Consequences

- A task that names its target in the request is served without hand-written `context_files`, which removes one way a harness limitation can look like a model failure.
- Ranking is lexical. A request that names no file selects nothing, and one that names a common word may still over-select. Two such weaknesses showed up in tests: a generic directory word pulled in a whole package, and a common extension (the `py` in `ops.py`) scored every Python file. Both are now guarded.
- The estimate and the attempt select through two code paths against two views of the same commit. They are pinned equal by a test, and a divergence is a real defect: too low overspends the cap, too high refuses a run the owner could afford.
- **A negative result is a likely outcome**, because a curated suite's hand-written list is a strong baseline. The decision to keep `auto` opt-in means a negative costs tooling and knowledge, not a regression. A negative must still be written into `docs/06-EVALUATION.md` and the CHANGELOG.

## Revisit when

The M0.9 experiment returns, whether it accepts or rejects `auto`; if it accepts, when `arpeggio run` moves off `manual`. Also revisit if a selector bug is ever found to have made a file *unavailable* rather than merely unshown — that would mean the "selection is not authorisation" boundary was crossed.
