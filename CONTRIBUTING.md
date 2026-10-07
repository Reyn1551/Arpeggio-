# Contributing

Arpeggio is a personal project; this file exists so future collaborators (human or agent) follow the same workflow.

1. Pick a requirement ID from `docs/02-REQUIREMENTS.md` within the current roadmap milestone (`docs/08-ROADMAP.md`).
2. Create a branch `feat/<req-id>-<short-name>`.
3. Follow `AGENTS.md` conventions; write tests first for rule logic.
4. Run lint, types, tests; run `arpeggio eval gate` if the change touches routing, cost, verify, or intake.
5. Open a PR referencing requirement IDs and, if relevant, the eval run ID.
