"""Helpers for eval integration tests: write a task file and run a self-check."""

import asyncio
from pathlib import Path
from typing import Any

import yaml
from samplerepo import sample_suite

from arpeggio_ai.evals.selfcheck import CheckRun, TaskCheck
from arpeggio_ai.evals.suite import TaskEntry, discover
from arpeggio_ai.evals.task import substitute
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.store.artifacts import ArtifactStore

ENV = scrubbed_env()


class Suite:
    def __init__(self, tmp: Path, home: Path) -> None:
        self.repo, self.root, self.variables = sample_suite(tmp)
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        self.artifacts = ArtifactStore(home / "artifacts")

    def sha(self, name: str) -> str:
        return self.variables[f"SAMPLE_{name.upper()}"]

    def base_task(self, task_id: str = "custom-task", **changes: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": task_id,
            "category": "bugfix",
            "expected_risk": "low",
            "repo": {"path": "${SAMPLE_REPO}", "base": "${SAMPLE_C0}", "solution": "${SAMPLE_C1}"},
            "request": "Make calc.ops.add return the sum.",
            "done_criteria": [
                {
                    "kind": "command",
                    "argv": ["${PYTHON}", "-m", "unittest", "discover", "-s", "tests"],
                }
            ],
        }
        data.update(changes)
        return data

    def write_task(self, data: dict[str, Any], split_dir: str = "tasks") -> Path:
        file = self.root / split_dir / f"{data['id']}.yaml"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(yaml.safe_dump(data), encoding="utf-8")
        return file

    def write_fixture(self, relative: str, data: bytes) -> str:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return relative

    def entry(self, task_id: str) -> TaskEntry:
        found = [e for e in discover(self.root, self.variables) if e.id == task_id]
        assert len(found) == 1
        return found[0]

    def run(self, keep_worktrees: bool = False) -> CheckRun:
        return CheckRun(
            self.root, self.home, self.artifacts, lambda _t: ENV, keep_worktrees=keep_worktrees
        )

    def check(self, task_id: str, keep_worktrees: bool = False) -> TaskCheck:
        return asyncio.run(self.run(keep_worktrees).check(self.entry(task_id), 0))

    def resolved(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = substitute(data, self.variables)
        return result
