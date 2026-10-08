"""Harness for eval run tests: the sample suite, a scripted fake model and a temp DB.

``tier_model`` answers by tier: tier1 replies without a diff (``patch_missing``), tier2
fixes ``add`` but writes a wrong ``mul``, tier3 solves both. Nothing touches the network.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from evalkit import Suite
from fakes import FAKE_KEY, MONDAY_OFFPEAK, FakeSleep, deepseek_usage, ok
from routekit import build_config

from arpeggio_ai.adapters.base import AdapterContext
from arpeggio_ai.config.models import Config
from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.evals.budget import SpendCap
from arpeggio_ai.evals.runner import EvalRunner, RunOutcome, RunPlan, build_plan
from arpeggio_ai.evals.strategies import STRATEGIES, Strategy
from arpeggio_ai.evals.suite import REPORTS_DIR, discover, select
from arpeggio_ai.orchestrator.attempts import overhead_history
from arpeggio_ai.paths import db_path, logs_dir
from arpeggio_ai.routing.policy import load_policy
from arpeggio_ai.store.db import open_db

KEY_ENV = {"DEEP_KEY": FAKE_KEY}

FIX_ADD = """```diff
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
```"""


def mul_patch(body: str) -> str:
    return f"""```diff
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,2 +1,6 @@
 def add(a, b):
     return a + b
+
+
+def mul(a, b):
+    return {body}
```"""


Reply = Callable[[str, str], httpx.Response]  # (provider model id, user prompt) -> response


def tier_model(model: str, prompt: str) -> httpx.Response:
    usage = deepseek_usage(800, 0, 120)
    if "tier1" in model:
        return ok("I think the fix is easy.", usage=usage, model=model)
    if "product" in prompt:
        return ok(mul_patch("a * b" if "tier3" in model else "a + b"), usage=usage, model=model)
    return ok(FIX_ADD, usage=usage, model=model)


class FakeModel:
    def __init__(self, reply: Reply = tier_model) -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        user = next(m["content"] for m in body["messages"] if m["role"] == "user")
        return self.reply(str(body["model"]), str(user))

    def models(self) -> list[str]:
        return [str(body["model"]) for body in self.requests]


def write_report(root: Path, statuses: dict[str, str], at: datetime | None = None) -> Path:
    """A self-check report as ``eval check`` writes it, newer than every task file."""
    time.sleep(0.05)
    at = at or datetime.now(UTC)
    reports = root / REPORTS_DIR
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"check-{at:%Y%m%dT%H%M%S%f}Z.json"
    tasks = [{"id": task_id, "status": status} for task_id, status in statuses.items()]
    path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return path


class RunHarness:
    def __init__(self, tmp: Path, home: Path, config: Config | None = None) -> None:
        self.suite = Suite(tmp, home)
        self.home = home
        self.root = self.suite.root
        self.config = config or build_config()
        self.conn = open_db(db_path(home))
        configure_logging(logs_dir(home))
        self.model = FakeModel()
        self.policy, self.policy_source = load_policy(home)

    def close(self) -> None:
        self.conn.close()

    def valid(self, *ids: str) -> None:
        write_report(self.root, dict.fromkeys(ids, "valid"))

    def plan(
        self,
        ids: list[str] | None = None,
        strategies: tuple[Strategy, ...] = STRATEGIES,
        repeats: int = 1,
        split: str = "tuning",
        at: datetime = MONDAY_OFFPEAK,
    ) -> RunPlan:
        entries = select(discover(self.root, self.suite.variables), split, ids)
        return asyncio.run(
            build_plan(
                entries,
                evals_root=self.root,
                strategies=strategies,
                split=split,  # type: ignore[arg-type]
                repeats=repeats,
                config_for=lambda _repo: self.config,
                policy=self.policy,
                policy_source=self.policy_source,
                home=self.home,
                at=at,
            )
        )

    def context(self, config: Config) -> AdapterContext:
        return AdapterContext(
            config=config,
            clock=lambda: MONDAY_OFFPEAK,
            sleep=FakeSleep(),
            random=lambda: 0.5,
            env=KEY_ENV,
            transport=httpx.MockTransport(self.model.handle),
            prompt_overhead=overhead_history(self.conn),
        )

    def run(self, plan: RunPlan, limit_usd: float = 10.0, **kwargs: Any) -> RunOutcome:
        from arpeggio_ai.store.artifacts import ArtifactStore

        runner = EvalRunner(
            self.conn,
            ArtifactStore(self.home / "artifacts"),
            home=self.home,
            evals_root=self.root,
            context_for=self.context,
            cap=SpendCap(limit_usd),
            git_sha="0" * 40,
            config_hash="test",
            profile=self.config.budget.profile,
            **kwargs,
        )
        return asyncio.run(runner.run(plan))

    def worktrees(self) -> list[str]:
        root = self.home / "worktrees"
        return sorted(os.listdir(root)) if root.exists() else []
