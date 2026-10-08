"""The eval suite on disk: discovery across the two splits and the latest self-check status.

Layout of an evals directory::

    tasks/<id>.yaml       tuning split
    holdout/<id>.yaml     holdout split (EVL-02)
    fixtures/<id>/...     hidden tests and reference diffs
    .reports/             self-check reports, check-<UTC timestamp>.json
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from arpeggio_ai.evals.task import SPLIT_DIRS, EvalTask, Split, TaskError, load_task

REPORTS_DIR = ".reports"
HOLDOUT_MIN_SHARE = 0.30
SUITE_MIN_TASKS = 20


@dataclass(slots=True)
class TaskEntry:
    id: str
    split: Split
    file: Path
    task: EvalTask | None = None
    problems: list[str] = field(default_factory=list)


def discover(evals_root: Path, variables: Mapping[str, str] | None = None) -> list[TaskEntry]:
    """Every ``*.yaml`` in both splits, loaded and validated, sorted by split then ID.

    An ID present in both splits is a problem on each entry.
    """
    entries: list[TaskEntry] = []
    for split, dirname in SPLIT_DIRS.items():
        for file in sorted((evals_root / dirname).glob("*.yaml")):
            entry = TaskEntry(file.stem, split, file)
            try:
                entry.task = load_task(file, evals_root, variables)
            except TaskError as error:
                entry.problems = error.problems
            entries.append(entry)
    seen: dict[str, list[TaskEntry]] = {}
    for entry in entries:
        seen.setdefault(entry.id, []).append(entry)
    for same in seen.values():
        if len(same) > 1:
            for entry in same:
                entry.task = None
                entry.problems.append(f"id: '{entry.id}' exists in both splits")
    return entries


def select(
    entries: list[TaskEntry], split: str = "all", ids: list[str] | None = None
) -> list[TaskEntry]:
    chosen = [entry for entry in entries if split in ("all", entry.split)]
    if ids:
        chosen = [entry for entry in chosen if entry.id in ids]
    return chosen


_REPORT = re.compile(r"check-[0-9TZ-]+\.json")


def latest_statuses(evals_root: Path) -> dict[str, str]:
    """For each task ID, its status in the newest report that mentions it."""
    return {task_id: status for task_id, (status, _) in latest_checks(evals_root).items()}


def _report_time(name: str) -> datetime | None:
    stamp = name.removeprefix("check-").removesuffix(".json")
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def latest_checks(evals_root: Path) -> dict[str, tuple[str, datetime | None]]:
    """For each task ID, its status and the time of the newest report that mentions it."""
    statuses: dict[str, tuple[str, datetime | None]] = {}
    reports = sorted(
        (p for p in (evals_root / REPORTS_DIR).glob("check-*.json") if _REPORT.fullmatch(p.name)),
        reverse=True,
    )
    for report in reports:
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for task in data.get("tasks", []):
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                statuses.setdefault(task["id"], (str(task.get("status")), _report_time(report.name)))
    return statuses


def referenced_files(entry: TaskEntry, evals_root: Path) -> list[Path]:
    """The task file and every file it references in the evals dir."""
    files = [entry.file]
    task = entry.task
    if task is not None:
        relative = [test.source for test in task.hidden_tests]
        if task.reference_diff is not None:
            relative.append(task.reference_diff)
        files += [evals_root / path for path in relative]
    return files


def run_status(entry: TaskEntry, evals_root: Path, checks: dict[str, tuple[str, datetime | None]]) -> str:
    """``valid`` only if the latest self-check said so and is newer than the task file and
    every file it references; ``stale_self_check`` if any of them changed since;
    ``unchecked``, ``invalid_schema`` or the failing status otherwise."""
    if entry.task is None:
        return "invalid_schema"
    status, checked_at = checks.get(entry.id, ("unchecked", None))
    if status != "valid":
        return status
    if checked_at is None:
        return "stale_self_check"
    for file in referenced_files(entry, evals_root):
        try:
            modified = datetime.fromtimestamp(file.stat().st_mtime, UTC)
        except OSError:
            return "stale_self_check"
        if modified >= checked_at:
            return "stale_self_check"
    return "valid"


@dataclass(frozen=True, slots=True)
class SuiteSummary:
    tuning: int
    holdout: int
    warnings: list[str]

    @property
    def total(self) -> int:
        return self.tuning + self.holdout

    @property
    def holdout_share(self) -> float:
        return self.holdout / self.total if self.total else 0.0


def summarize(entries: list[TaskEntry]) -> SuiteSummary:
    """Counts per split and the EVL-01 / EVL-02 warnings (≥ 20 tasks, ≥ 30% holdout)."""
    tuning = sum(1 for entry in entries if entry.split == "tuning")
    holdout = len(entries) - tuning
    summary = SuiteSummary(tuning, holdout, [])
    if summary.total < SUITE_MIN_TASKS:
        summary.warnings.append(
            f"only {summary.total} tasks; EVL-01 asks for at least {SUITE_MIN_TASKS}"
        )
    if summary.holdout_share < HOLDOUT_MIN_SHARE:
        summary.warnings.append(
            f"holdout share {summary.holdout_share:.0%} is below the "
            f"{HOLDOUT_MIN_SHARE:.0%} EVL-02 asks for"
        )
    return summary
