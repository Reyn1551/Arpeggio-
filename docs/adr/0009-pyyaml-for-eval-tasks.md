# ADR-0009: PyYAML for eval task files

- **Status:** accepted
- **Date:** 2026-10-08

## Context

Milestone M0.5 adds the eval suite. Its task files are YAML, as [06-EVALUATION](../06-EVALUATION.md) has described since the start: the owner writes and edits them by hand, and YAML allows comments and folded text blocks for the `request`. Python's standard library has no YAML parser, and the [AGENTS.md](../../AGENTS.md) hard rules require an ADR for a new dependency.

Task files come from the owner's own evals directory, but a task file is still data, and a YAML loader that can build arbitrary Python objects would turn a bad file into code execution.

## Decision

Add `pyyaml` as a runtime dependency (and `types-PyYAML` as a dev dependency for `mypy --strict`). Task files are read only with `yaml.safe_load`, which builds plain dicts, lists, strings, numbers, booleans and null, and the result is then validated by a Pydantic model with `extra="forbid"`. `eval new` writes task files as text from a fixed template, so the comments it puts there survive.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| TOML (`tomllib`, stdlib) | No dependency, already used for config | Task format in docs/06 is YAML, multi-line `request` text and lists of tables read worse |
| JSON | No dependency | No comments, hand editing is error prone |
| `ruamel.yaml` | Round-trips comments | Larger, and round-tripping is not needed because `eval new` writes from a template |
| **`pyyaml` with `safe_load` (chosen)** | Small, widely packaged, wheels for every platform | Its full loader is unsafe, so `safe_load` must be the only entry point |

## Consequences

One more runtime dependency in the lock file. A grep for `yaml.load(` and `yaml.Loader` must stay empty. Task files that use YAML features beyond plain data (tags, anchors to objects) are rejected.

## Revisit when

A task file needs a feature `safe_load` does not support, or task files start to be written by code that must preserve comments.
