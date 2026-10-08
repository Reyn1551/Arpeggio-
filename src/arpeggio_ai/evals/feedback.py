"""Compact failure report for an escalated attempt (VER-03), without leaking hidden tests.

The next attempt's prompt gets: the attempt number, the failure kind (``checks_failed``,
``output_truncated`` or a ``patch_*`` reason), for ``output_truncated`` one line asking for
short reasoning (``TRUNCATED_HINT``), each failing criterion's 1-based index and exit code, and the last
``FEEDBACK_TAIL_CHARS`` characters of its already redacted output, but only when that
output cannot show a hidden test. Never the previous transcript, never hidden test files.

Output is withheld for a criterion when the task has hidden tests and any of these holds:

- an argument names a hidden test path, one of its parent directories (``tests``, ``.``),
  or its dotted module form (``tests.test_mul``), compared case-insensitively,
- no argument names an explicit path or module at all (``pytest``, ``npm test``), since
  such a runner may discover the hidden tests on its own,
- the output itself mentions a hidden test path or file name.

This is stricter than "argv does not contain a hidden path" on purpose: a test runner that
discovers tests by directory would otherwise print hidden assertions.
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass

FEEDBACK_TAIL_CHARS = 1500
TRUNCATED_HINT = (
    "The reply was cut off at the output limit before a usable diff."
    " Keep reasoning short and write the diff first."
)
_EXPLICIT = re.compile(r"[^-][^=]*[/\\].*|[^-][\w-]*\.[\w.-]+")


@dataclass(frozen=True, slots=True)
class FailedCheck:
    index: int  # 1-based, like check-<n>.log
    exit_code: int | None
    argv: Sequence[str] | None  # None for file_exists criteria
    output_tail: str


def _norm(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/").casefold()


def _hidden_forms(hidden: Collection[str]) -> tuple[set[str], set[str]]:
    """(paths, parents and module forms) of the hidden tests, normalized."""
    paths = {_norm(path) for path in hidden}
    related: set[str] = {"", "."}
    for path in paths:
        parts = path.split("/")
        related.update("/".join(parts[:n]) for n in range(1, len(parts)))
        related.add(path.rsplit(".", 1)[0].replace("/", "."))
    return paths, related


def output_may_show_hidden(argv: Sequence[str] | None, output: str, hidden: Collection[str]) -> bool:
    if not hidden:
        return False
    paths, related = _hidden_forms(hidden)
    names = {path.rsplit("/", 1)[-1] for path in paths}
    lowered = output.replace("\\", "/").casefold()
    if any(name in lowered for name in names):
        return True
    if argv is None:
        return False
    explicit = False
    for token in argv[1:]:
        value = token.split("=", 1)[1] if token.startswith("-") and "=" in token else token
        norm = _norm(value)
        if norm in paths or norm in related:
            return True
        if _EXPLICIT.fullmatch(value):
            explicit = True
    return not explicit


def failure_report(
    attempt_number: int,
    kind: str,
    failed: Sequence[FailedCheck],
    hidden: Collection[str],
) -> str:
    """The section appended to the next attempt's prompt."""
    lines = [f"Attempt {attempt_number} failed: {kind}."]
    if kind == "checks_failed":
        lines.append("Failing checks (numbered as listed above):")
    elif kind == "output_truncated":
        lines.append(TRUNCATED_HINT)
    for check in failed:
        code = "none (did not finish)" if check.exit_code is None else str(check.exit_code)
        lines.append(f"- check {check.index}: exit code {code}")
        tail = check.output_tail[-FEEDBACK_TAIL_CHARS:]
        if output_may_show_hidden(check.argv, check.output_tail, hidden):
            lines.append("  output withheld")
        elif tail.strip():
            lines.append(f"  last output:\n````\n{tail}\n````")
    lines.append("Write a new diff against the original files, not on top of the failed one.")
    return "\n".join(lines)
