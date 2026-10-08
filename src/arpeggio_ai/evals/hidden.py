"""Hidden tests (SWE-bench pattern): copied into the worktree only for verification.

They are injected after any patch is applied and before the checks run. A model never
sees them: ``hidden_paths`` feeds the context guard in ``orchestrator.patch.read_context``.
"""

import logging
from pathlib import Path

from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.evals.task import EvalTask, inside

log = logging.getLogger(__name__)


class HiddenTestError(ArpeggioError):
    """A hidden test could not be copied into the worktree."""


def hidden_paths(task: EvalTask | None) -> frozenset[str]:
    return frozenset() if task is None else frozenset(test.path for test in task.hidden_tests)


def inject_hidden_tests(worktree: Path, task: EvalTask, evals_root: Path) -> list[str]:
    """Copy each hidden test to its path in ``worktree``. Returns the paths it overwrote."""
    overwritten = []
    for test in task.hidden_tests:
        source = inside(evals_root, test.source)
        target = inside(worktree, test.path)
        if source is None or not source.is_file():
            raise HiddenTestError(f"hidden test source not found: {test.source}")
        if target is None:
            raise HiddenTestError(f"hidden test path leaves the worktree: {test.path}")
        if target.exists():
            overwritten.append(test.path)
            log.warning("eval.hidden_test_overwritten", extra={"path": test.path})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    log.info("eval.hidden_tests_injected", extra={"count": len(task.hidden_tests)})
    return overwritten
