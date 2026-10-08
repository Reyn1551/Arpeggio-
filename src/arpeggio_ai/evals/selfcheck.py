"""``arpeggio eval check``: prove each task discriminates before it counts (EVL-01).

Zero model calls. For every task:

1. **Base run**: worktree at ``base``, setup, hidden tests injected, criteria run. At
   least one criterion must fail, else the task is ``non_discriminating``.
2. **Solution run**: worktree at ``solution``, or at ``base`` with ``reference_diff``
   applied by the patch machinery of M0.4. Same steps. Every criterion must pass, else the
   task is ``unsolvable``.

Setup and checks run through ``run_process`` with the scrubbed environment, timeouts,
output caps, Job Objects and secret redaction of M0.4. Logs go to the artifact store under
``eval-check-<run_id>/<task_id>/``; the report under ``<evals>/.reports/`` holds statuses,
durations, failing criterion numbers (1-based, like ``check-<n>.log``) and artifact
references, never output.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from arpeggio_ai.core.clock import format_utc
from arpeggio_ai.core.ids import new_id
from arpeggio_ai.evals.hidden import HiddenTestError, inject_hidden_tests
from arpeggio_ai.evals.suite import REPORTS_DIR, TaskEntry
from arpeggio_ai.evals.task import EvalTask, inside
from arpeggio_ai.orchestrator.patch import PatchError, apply_patch
from arpeggio_ai.safety.process import ProcessError, run_process
from arpeggio_ai.safety.secret_scan import SecretScanner, use_scanner
from arpeggio_ai.safety.worktree import (
    WorktreeError,
    create_worktree,
    remove_worktree,
    resolve_commit,
)
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.verify.runner import run_specs

log = logging.getLogger(__name__)

Status = Literal[
    "valid", "invalid_schema", "non_discriminating", "unsolvable", "setup_failed", "error"
]
Phase = Literal["base", "solution"]

# Environment for one task's processes: built from the repo's config (check_env, provider
# key names) by the caller.
EnvFor = Callable[[EvalTask], Mapping[str, str]]
ScannerFor = Callable[[EvalTask], SecretScanner]


@dataclass(slots=True)
class PhaseResult:
    commit: str | None = None
    applied_reference_diff: bool = False
    setup_failed: bool = False
    failing: list[int] = field(default_factory=list)
    duration_s: float = 0.0
    logs: list[str] = field(default_factory=list)
    worktree: str | None = None


@dataclass(slots=True)
class TaskCheck:
    id: str
    split: str
    status: Status
    detail: str | None = None
    base: PhaseResult | None = None
    solution: PhaseResult | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _PhaseFailed(Exception):
    def __init__(self, status: Status, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(slots=True)
class CheckRun:
    evals_root: Path
    home: Path
    artifacts: ArtifactStore
    env_for: EnvFor
    scanner_for: ScannerFor = lambda _task: SecretScanner()
    keep_worktrees: bool = False
    run_id: str = field(default_factory=new_id)

    @property
    def artifact_group(self) -> str:
        return f"eval-check-{self.run_id}"

    async def _phase(self, task: EvalTask, index: int, phase: Phase, result: PhaseResult) -> None:
        env = self.env_for(task)
        repo = Path(task.repo.path)
        use_diff = phase == "solution" and task.repo.solution is None
        ref = task.repo.base if phase == "base" or use_diff else task.repo.solution
        assert ref is not None  # the model requires solution or reference_diff
        short = self.run_id[-8:].lower()
        try:
            result.commit = await resolve_commit(repo, ref, home=self.home, env=env)
            tree = await create_worktree(
                repo,
                self.home,
                task_id=task.id,
                attempt_id=f"ev{short}{index:03d}{phase[0]}",
                attempt_seq=0,
                env=env,
                base=result.commit,
                branch=f"arpeggio/eval/{task.id}/{short}-{phase}",
            )
        except WorktreeError as error:
            raise _PhaseFailed("error", f"{phase}: {error}") from None
        result.worktree = str(tree.path)

        def write_log(name: str, data: bytes) -> str:
            ref = self.artifacts.write(self.artifact_group, task.id, f"{phase}-{name}", data)
            result.logs.append(ref)
            return ref

        try:
            if use_diff:
                assert task.reference_diff is not None
                source = inside(self.evals_root, task.reference_diff)
                assert source is not None  # checked by load_task
                try:
                    await apply_patch(
                        source.read_text(encoding="utf-8"),
                        tree.path,
                        attempt_id=f"eval-{task.id}",
                        env=env,
                        home=self.home,
                    )
                except PatchError as error:
                    raise _PhaseFailed(
                        "unsolvable", f"reference_diff does not apply: {error.detail}"
                    ) from None
                result.applied_reference_diff = True
            for number, step in enumerate(task.setup, start=1):
                name = f"setup-{number}.log"
                try:
                    run = await run_process(
                        step.argv, cwd=tree.path, env=env, timeout_s=step.timeout_s
                    )
                except ProcessError as error:
                    write_log(name, str(error).encode("utf-8"))
                    result.setup_failed = True
                    raise _PhaseFailed("setup_failed", f"{phase} setup {number}: {error}") from None
                write_log(name, run.output)
                if run.timed_out or run.output_limit_exceeded or run.exit_code != 0:
                    result.setup_failed = True
                    why = "timed out" if run.timed_out else f"exit code {run.exit_code}"
                    raise _PhaseFailed("setup_failed", f"{phase} setup {number}: {why}")
            try:
                inject_hidden_tests(tree.path, task, self.evals_root)
            except HiddenTestError as error:
                raise _PhaseFailed("error", f"{phase}: {error}") from None
            checks = await run_specs(
                task.criteria, worktree=tree.path, env=env, write_log=write_log
            )
            result.failing = [n for n, check in enumerate(checks, start=1) if not check.passed]
        finally:
            if not self.keep_worktrees:
                await remove_worktree(repo, self.home, tree.path, tree.branch, env)
                result.worktree = None

    async def check(self, entry: TaskEntry, index: int) -> TaskCheck:
        if entry.task is None:
            return TaskCheck(entry.id, entry.split, "invalid_schema", "; ".join(entry.problems))
        task = entry.task
        outcome = TaskCheck(entry.id, entry.split, "valid")
        try:
            with use_scanner(self.scanner_for(task)):
                for phase in ("base", "solution"):
                    result = PhaseResult()
                    setattr(outcome, phase, result)
                    started = time.monotonic()
                    try:
                        await self._phase(task, index, phase, result)
                    finally:
                        result.duration_s = round(time.monotonic() - started, 3)
        except _PhaseFailed as failure:
            outcome.status, outcome.detail = failure.status, failure.detail
        except Exception as error:  # reported per task, the other tasks still run
            log.exception("eval.check_error", extra={"eval_task": task.id})
            outcome.status, outcome.detail = "error", f"{type(error).__name__}: {error}"
        else:
            assert outcome.base is not None and outcome.solution is not None
            if not outcome.base.failing:
                outcome.status = "non_discriminating"
                outcome.detail = "every criterion already passes at base"
            elif outcome.solution.failing:
                outcome.status = "unsolvable"
                outcome.detail = f"criteria {outcome.solution.failing} fail with the solution"
        log.info("eval.task_checked", extra={"eval_task": task.id, "status": outcome.status})
        return outcome

    def write_report(self, checks: list[TaskCheck]) -> Path:
        now = datetime.now(UTC)
        reports = self.evals_root / REPORTS_DIR
        reports.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        path = reports / f"check-{stamp}.json"
        payload = {
            "run_id": self.run_id,
            "created_at": format_utc(now),
            "artifacts": self.artifact_group,
            "tasks": [check.to_dict() for check in checks],
        }
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        return path


async def check_entries(run: CheckRun, entries: list[TaskEntry]) -> list[TaskCheck]:
    return [await run.check(entry, index) for index, entry in enumerate(entries)]
