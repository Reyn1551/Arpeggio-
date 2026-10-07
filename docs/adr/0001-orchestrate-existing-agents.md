# ADR-0001: Orchestrate existing agents instead of building an agent loop

- **Status:** accepted
- **Date:** 2026-10-07

## Context

Mature coding agents (Claude Code, opencode, Command Code) already solve tool use, file editing, and context handling well, and are improved continuously by their vendors. Matching them with a homegrown loop would consume most of the project's time without differentiating it. What none of them offers is cross-vendor routing, cost governance, independent verification, and vendor-neutral learning.

## Decision

Arpeggio treats agents as executors behind one `Adapter` interface. Arpeggio's own value lives in intake, routing, cost governance, verification, approvals, and learning. A minimal "direct API" adapter exists for simple single-shot or short tasks where a full agent is overkill.

## Alternatives considered

| Option | Pros | Cons |
|---|---|---|
| Build own agent loop | Full control, exact token accounting | Huge effort, constantly behind vendors |
| Fork opencode | Open source, provider-agnostic | Locks Arpeggio to one codebase's architecture; harder to orchestrate other agents |
| **Orchestrate agents (chosen)** | Leverage vendor improvements; focus on differentiators | Less control inside attempts; CLI output formats can change; some adapters only give estimated costs |

## Consequences

- Adapters must be resilient to CLI output changes; contract tests catch breakage.
- Cost may be estimated for some adapters (flagged in data).
- Effort control depends on each adapter's capabilities; the router must respect `capabilities()`.

## Revisit when

Adapter breakage costs more than a week per quarter, or a needed capability (e.g. effort control, exact cost) is unavailable in every agent for a major task category.
