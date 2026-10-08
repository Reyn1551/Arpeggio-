"""Run a task's done criteria in an attempt worktree and store one verdict per check.

An attempt is judged only by these checks, never by what the model says (VER-01). Every
criterion runs, in stored order, even after a failure, so the verdicts show the whole
picture (VER-04). Command output goes to an artifact ``check-<n>.log``, and the verdict
keeps the exit code, duration, the timeout and output-limit flags and the last 2,000
characters of output. Output is redacted by the secret scanner before the tail is cut
(NFR-06).

``run_specs`` does the same without a database: eval self-checks use it with a callback that
stores each log wherever the caller wants.
"""

import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arpeggio_ai.safety.process import ProcessError, run_process
from arpeggio_ai.store.artifacts import ArtifactStore, redact_artifact
from arpeggio_ai.store.repositories import Verdict, add_verdict, list_criteria
from arpeggio_ai.verify.criteria import CommandCriterion, FileExistsCriterion

Spec = CommandCriterion | FileExistsCriterion
WriteLog = Callable[[str, bytes], str]

log = logging.getLogger(__name__)

OUTPUT_TAIL_CHARS = 2000


async def _run_command(
    criterion: CommandCriterion, worktree: Path, env: Mapping[str, str], log_name: str
) -> tuple[bool, dict[str, Any], bytes]:
    try:
        result = await run_process(
            criterion.argv, cwd=worktree, env=env, timeout_s=criterion.timeout_s
        )
    except ProcessError as error:
        detail = {
            "exit_code": None,
            "expect_exit": criterion.expect_exit,
            "duration_s": 0.0,
            "timed_out": False,
            "output_limit_exceeded": False,
            "error": str(error),
            "output_tail": str(error),
        }
        return False, detail, str(error).encode("utf-8")
    passed = (
        not result.timed_out
        and not result.output_limit_exceeded
        and result.exit_code == criterion.expect_exit
    )
    # Redact once, before the tail is cut, so the verdict never holds part of a secret.
    output = redact_artifact(log_name, result.output)
    detail = {
        "exit_code": result.exit_code,
        "expect_exit": criterion.expect_exit,
        "duration_s": result.duration_s,
        "timed_out": result.timed_out,
        "output_limit_exceeded": result.output_limit_exceeded,
        "output_tail": output.decode("utf-8", errors="replace")[-OUTPUT_TAIL_CHARS:],
    }
    return passed, detail, output


def _file_exists(criterion: FileExistsCriterion, worktree: Path) -> tuple[bool, dict[str, Any]]:
    root = worktree.resolve()
    target = (worktree / criterion.path).resolve()
    if not target.is_relative_to(root):
        return False, {
            "path": criterion.path,
            "exists": False,
            "error": "resolves outside the worktree",
        }
    return target.exists(), {"path": criterion.path, "exists": target.exists()}


@dataclass(frozen=True, slots=True)
class CheckResult:
    passed: bool
    detail: dict[str, Any]
    log_ref: str | None


async def _run_spec(
    number: int, criterion: Spec, worktree: Path, env: Mapping[str, str], write_log: WriteLog
) -> CheckResult:
    log_ref = None
    if isinstance(criterion, CommandCriterion):
        name = f"check-{number}.log"
        passed, detail, output = await _run_command(criterion, worktree, env, name)
        log_ref = write_log(name, output)
    else:
        passed, detail = _file_exists(criterion, worktree)
    log.info(
        "verify.check",
        extra={
            "check": number,
            "criterion_kind": criterion.kind,
            "passed": passed,
            "timed_out": bool(detail.get("timed_out")),
        },
    )
    return CheckResult(passed, detail, log_ref)


async def run_specs(
    specs: Sequence[Spec], *, worktree: Path, env: Mapping[str, str], write_log: WriteLog
) -> list[CheckResult]:
    """Run every spec in order, even after a failure. ``write_log(name, output)`` stores a
    command's redacted output and returns its reference."""
    return [
        await _run_spec(number, criterion, worktree, env, write_log)
        for number, criterion in enumerate(specs, start=1)
    ]


async def run_criteria(
    conn: sqlite3.Connection,
    artifacts: ArtifactStore,
    *,
    task_id: str,
    attempt_id: str,
    worktree: Path,
    env: Mapping[str, str],
) -> list[Verdict]:
    """Run every criterion of the task, store a verdict for each, and return them in order.

    Each verdict is committed in its own transaction as soon as its check finishes, so a
    crash later in the run never loses the verdicts already reached (NFR-03).
    """

    def write_log(name: str, data: bytes) -> str:
        return artifacts.write(task_id, attempt_id, name, data)

    verdicts: list[Verdict] = []
    for number, row in enumerate(list_criteria(conn, task_id), start=1):
        result = await _run_spec(number, row.parsed(), worktree, env, write_log)
        verdicts.append(
            add_verdict(
                conn,
                attempt_id,
                kind="check",
                passed=result.passed,
                criterion_id=row.id,
                detail=result.detail,
                log_ref=result.log_ref,
            )
        )
    return verdicts
