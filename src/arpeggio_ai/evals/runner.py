"""``arpeggio eval run``: the baseline experiment (EVL-03, EVL-06, VER-03, CST-05).

Planning (``build_plan``) makes no model call and creates no worktree, so it backs
``--dry-run``: it selects tasks whose self-check is current and ``valid``, resolves every
strategy's route sequence per task under that task's merged config (profile, SAF-07,
placeholders), and estimates the worst-case cost (``evals/budget.py``).

Running (``EvalRunner``) creates one ``eval_runs`` row per strategy. Tasks are run task by
task, repeat by repeat, and the strategies interleaved within each, so a run stopped by the
cap still has paired results. Each task run is one ``tasks`` row (``source = 'eval'``) with
the eval task's criteria; each route tried is one single-shot patch attempt (ADR-0008).
After the patch is applied and before the checks, the task's ``setup`` commands run and its
hidden tests are copied in. An attempt that fails its checks, whose patch fails
(``patch_*``) or whose reply was cut off before a usable diff (``output_truncated``) escalates to the next route with a compact failure report
(``evals/feedback.py``). Provider errors, timeouts, setup failures and guard refusals do
not escalate. Worktrees are removed after each attempt unless ``keep_worktrees``.
"""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from arpeggio_ai import __version__
from arpeggio_ai.adapters.base import AdapterContext, Route
from arpeggio_ai.config.models import Config
from arpeggio_ai.core.errors import ArpeggioError, ConfigError
from arpeggio_ai.cost.pricing import compute_cost, round_usd, snapshot
from arpeggio_ai.evals.budget import (
    SpendCap,
    context_bytes,
    prompt_chars,
    worst_case_usd,
)
from arpeggio_ai.evals.feedback import FailedCheck, failure_report
from arpeggio_ai.evals.hidden import HiddenTestError, hidden_paths, inject_hidden_tests
from arpeggio_ai.evals.strategies import PlannedRoute, Strategy, plan_routes
from arpeggio_ai.evals.suite import TaskEntry, latest_checks, run_status
from arpeggio_ai.evals.task import EvalTask, Split
from arpeggio_ai.orchestrator.attempts import PatchAttemptOutcome, PrepareError, run_patch_attempt
from arpeggio_ai.routing.policy import PolicyFile
from arpeggio_ai.routing.resolve import NoAllowedRoute
from arpeggio_ai.safety.process import ProcessError, provider_key_names, run_process, scrubbed_env
from arpeggio_ai.safety.worktree import WorktreeError, remove_worktree, resolve_commit
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.repositories import (
    EvalResultStatus,
    EvalRun,
    EvalRunStatus,
    EvalSplit,
    Step,
    add_criterion,
    add_eval_result,
    create_eval_run,
    create_task,
    finish_eval_run,
    get_attempt,
    list_criteria,
    list_steps,
    list_task_attempts,
    register_repo,
    set_counterfactual,
    task_cost_usd,
)

log = logging.getLogger(__name__)

ConfigFor = Callable[[Path], Config]  # repo path -> global config merged with the repo's
ContextFor = Callable[[Config], AdapterContext]
OverheadFor = Callable[[str, str], int]  # (provider, model key) -> observed overhead
Sleep = Callable[[float], Awaitable[None]]
REASON_MAX_CHARS = 500


class EvalRunError(ArpeggioError):
    """The eval run cannot be planned as asked."""


def env_for(config: Config) -> dict[str, str]:
    return scrubbed_env(extra=config.repo.check_env, deny=provider_key_names(config))


# Planning


@dataclass(slots=True)
class StrategyPlan:
    routes: list[PlannedRoute] = field(default_factory=list)
    estimates_usd: list[float] = field(default_factory=list)  # worst case per route
    skip: str | None = None  # why this strategy cannot run the task

    @property
    def estimate_usd(self) -> float:
        return float(sum(Decimal(repr(value)) for value in self.estimates_usd))


@dataclass(slots=True)
class TaskPlan:
    entry: TaskEntry
    task: EvalTask
    config: Config
    strategies: dict[Strategy, StrategyPlan]


@dataclass(frozen=True, slots=True)
class SkippedTask:
    id: str
    split: Split
    reason: str  # stale_self_check, unchecked, a failing self-check status, config_error
    message: str


@dataclass(slots=True)
class RunPlan:
    tasks: list[TaskPlan]
    skipped: list[SkippedTask]
    strategies: tuple[Strategy, ...]
    repeats: int
    split: EvalSplit
    at: datetime
    policy_source: str

    def strategy_estimate_usd(self, strategy: Strategy) -> float:
        total = sum(
            (Decimal(repr(t.strategies[strategy].estimate_usd)) for t in self.tasks), Decimal(0)
        )
        return round_usd(total * self.repeats)

    @property
    def estimate_usd(self) -> float:
        total = sum(
            (Decimal(repr(self.strategy_estimate_usd(s))) for s in self.strategies), Decimal(0)
        )
        return round_usd(total)

    @property
    def planned(self) -> int:
        return len(self.tasks) * self.repeats

    def route_keys(self) -> list[tuple[Config, set[str]]]:
        return [
            (t.config, {r.model for p in t.strategies.values() for r in p.routes})
            for t in self.tasks
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategies": list(self.strategies),
            "split": self.split,
            "repeats": self.repeats,
            "planned_task_runs": self.planned,
            "estimate_usd": self.estimate_usd,
            "estimate_per_strategy_usd": {
                s: self.strategy_estimate_usd(s) for s in self.strategies
            },
            "policy": self.policy_source,
            "at": self.at.isoformat(),
            "tasks": [
                {
                    "id": t.task.id,
                    "split": t.entry.split,
                    "category": t.task.category,
                    "expected_risk": t.task.expected_risk,
                    "profile": t.config.budget.profile,
                    "strategies": {
                        s: {
                            "routes": [
                                {
                                    "model": r.model,
                                    "effort": r.effort,
                                    "requested_effort": r.requested_effort,
                                    **({"rule": r.reason["rule"]} if "rule" in r.reason else {}),
                                }
                                for r in p.routes
                            ],
                            "estimate_usd": round_usd(Decimal(repr(p.estimate_usd))),
                            "skip": p.skip,
                        }
                        for s, p in t.strategies.items()
                    },
                }
                for t in self.tasks
            ],
            "skipped": [
                {"id": s.id, "split": s.split, "reason": s.reason, "message": s.message}
                for s in self.skipped
            ],
        }


_SKIP_MESSAGES = {
    "stale_self_check": "the task or a file it references changed after its last self-check;"
    " run `arpeggio eval check --id {id}`",
    "unchecked": "never self-checked; run `arpeggio eval check --id {id}`",
}


async def build_plan(
    entries: list[TaskEntry],
    *,
    evals_root: Path,
    strategies: tuple[Strategy, ...],
    split: EvalSplit,
    repeats: int,
    config_for: ConfigFor,
    policy: PolicyFile,
    policy_source: str,
    home: Path,
    at: datetime,
    overhead: OverheadFor = lambda provider, model: 0,
) -> RunPlan:
    """Select runnable tasks and plan every strategy's routes and worst-case cost."""
    if repeats < 1:
        raise EvalRunError("--repeats must be >= 1")
    checks = latest_checks(evals_root)
    plan = RunPlan([], [], strategies, repeats, split, at, policy_source)
    for entry in entries:
        status = run_status(entry, evals_root, checks)
        if status != "valid" or entry.task is None:
            template = _SKIP_MESSAGES.get(status, "latest self-check status is {status}")
            message = template.format(id=entry.id, status=status)
            plan.skipped.append(SkippedTask(entry.id, entry.split, status, message))
            continue
        task = entry.task
        try:
            config = config_for(Path(task.repo.path))
        except ConfigError as error:
            plan.skipped.append(SkippedTask(entry.id, entry.split, "config_error", str(error)))
            continue
        env = env_for(config)
        size = await context_bytes(
            Path(task.repo.path),
            task.repo.base,
            task.context_files,
            hidden=hidden_paths(task),
            env=env,
            home=home,
        )
        per_strategy: dict[Strategy, StrategyPlan] = {}
        for strategy in strategies:
            try:
                routes = plan_routes(strategy, task, config, policy)
            except NoAllowedRoute as error:
                per_strategy[strategy] = StrategyPlan(skip=f"no_allowed_route: {error}")
                continue
            estimates = []
            for step, route in enumerate(routes):
                spec = config.models[route.model]
                estimates.append(
                    worst_case_usd(
                        config,
                        route.model,
                        chars=prompt_chars(task, size, escalated=step > 0),
                        max_tokens=config.evals.max_tokens,
                        at=at,
                        observed_overhead_tokens=overhead(spec.provider, route.model),
                    )
                )
            per_strategy[strategy] = StrategyPlan(routes, estimates)
        plan.tasks.append(TaskPlan(entry, task, config, per_strategy))
    return plan


# Running


def config_hash(config: Config) -> str:
    """Short SHA-256 of the effective global config, as validated."""
    data = json.dumps(config.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:16]


async def harness_sha(env: Mapping[str, str]) -> str:
    """The Arpeggio checkout's HEAD, or ``unknown-<version>`` for an installed package."""
    root = Path(__file__).resolve().parents[3]
    try:
        result = await run_process(["git", "rev-parse", "HEAD"], cwd=root, env=env, timeout_s=30)
    except ProcessError:
        return f"unknown-{__version__}"
    text = result.text().strip()
    if result.exit_code != 0 or len(text) != 40:
        return f"unknown-{__version__}"
    return text


def counterfactual_usd(config: Config, steps: list[Step]) -> float:
    """The task's model-call tokens priced on ``[defaults] counterfactual_route`` at each
    call's time (CST-05). Steps without token counts add nothing."""
    model = config.models[config.defaults.counterfactual_route.model]
    provider = config.providers[model.provider]
    total = Decimal(0)
    for step in steps:
        if step.kind != "model_call" or step.input_tokens is None:
            continue
        at = datetime.fromisoformat(step.created_at)
        cached = min(step.cached_tokens or 0, step.input_tokens)
        price = snapshot(model, provider, at)
        total += compute_cost(price, step.input_tokens, cached, step.output_tokens or 0)
    return round_usd(total)


def escalation_kind(outcome: PatchAttemptOutcome) -> str | None:
    """``checks_failed``, ``output_truncated`` or the ``patch_*`` reason when the next route
    should be tried."""
    reason = outcome.failure_reason
    if reason is not None and (reason.startswith("patch_") or reason == "output_truncated"):
        return reason
    if outcome.attempt_status == "completed" and outcome.task_status == "failed":
        return "checks_failed"
    return None


@dataclass(slots=True)
class RunOutcome:
    runs: dict[Strategy, EvalRun]
    status: EvalRunStatus
    reason: str | None
    spent_usd: float
    results: int = 0


@dataclass(slots=True)
class EvalRunner:
    conn: sqlite3.Connection
    artifacts: ArtifactStore
    home: Path
    evals_root: Path
    context_for: ContextFor
    cap: SpendCap
    git_sha: str
    config_hash: str
    profile: Any  # BudgetProfile of the global config
    keep_worktrees: bool = False
    sleep_between_s: float = 0.0
    sleep: Sleep = asyncio.sleep
    _attempts: int = 0
    _results: int = 0

    async def run(self, plan: RunPlan) -> RunOutcome:
        runs = {
            strategy: create_eval_run(
                self.conn,
                strategy=strategy,
                split=plan.split,
                git_sha=self.git_sha,
                config_hash=self.config_hash,
                profile=self.profile,
                repeats=plan.repeats,
                planned=plan.planned,
                estimate_usd=plan.strategy_estimate_usd(strategy),
            )
            for strategy in plan.strategies
        }
        try:
            for skipped in plan.skipped:
                for run in runs.values():
                    add_eval_result(
                        self.conn,
                        run.id,
                        skipped.id,
                        repeat_index=0,
                        status="skipped",
                        status_reason=f"{skipped.reason}: {skipped.message}"[:REASON_MAX_CHARS],
                    )
            for task_plan in plan.tasks:
                for repeat in range(plan.repeats):
                    for strategy in plan.strategies:
                        if self.cap.stopped:
                            break
                        await self._run_one(runs[strategy], strategy, task_plan, repeat)
        except BaseException as error:
            for run in runs.values():
                finish_eval_run(self.conn, run.id, "aborted", reason=type(error).__name__)
            raise
        status: EvalRunStatus = "partial" if self.cap.stopped else "completed"
        reason = None
        if self.cap.stopped:
            reason = (
                f"budget_cap: stopped at ${self.cap.spent_usd:.6f} of ${self.cap.limit_usd:.6f};"
                " the next attempt's worst case would exceed the cap"
            )
        finished = {
            strategy: finish_eval_run(
                self.conn,
                run.id,
                status,
                reason=reason,
                summary={"spent_usd": round(self.cap.spent_usd, 8)},
            )
            for strategy, run in runs.items()
        }
        return RunOutcome(finished, status, reason, self.cap.spent_usd, self._results)

    def _prepare(
        self, task: EvalTask, env: Mapping[str, str], task_id: str
    ) -> Callable[[Path, str], Awaitable[None]]:
        async def prepare(worktree: Path, attempt_id: str) -> None:
            for number, step in enumerate(task.setup, start=1):
                name = f"setup-{number}.log"
                try:
                    result = await run_process(
                        step.argv, cwd=worktree, env=env, timeout_s=step.timeout_s
                    )
                except ProcessError as error:
                    raise PrepareError("setup_failed", f"setup {number}: {error}") from None
                self.artifacts.write(task_id, attempt_id, name, result.output)
                if result.timed_out or result.output_limit_exceeded or result.exit_code != 0:
                    why = "timed out" if result.timed_out else f"exit code {result.exit_code}"
                    raise PrepareError("setup_failed", f"setup {number}: {why}")
            try:
                inject_hidden_tests(worktree, task, self.evals_root)
            except HiddenTestError as error:
                raise PrepareError("hidden_tests_failed", str(error)) from None

        return prepare

    async def _remove(self, task: EvalTask, attempt_id: str, env: Mapping[str, str]) -> None:
        attempt = get_attempt(self.conn, attempt_id)
        if self.keep_worktrees or attempt is None or not attempt.worktree or not attempt.branch:
            return
        try:
            await remove_worktree(
                Path(task.repo.path), self.home, Path(attempt.worktree), attempt.branch, env
            )
        except WorktreeError:
            log.warning("eval.worktree_not_removed", extra={"attempt_id": attempt_id})

    def _failed_checks(self, task_id: str, outcome: PatchAttemptOutcome) -> list[FailedCheck]:
        criteria = {row.id: (n, row) for n, row in enumerate(list_criteria(self.conn, task_id), 1)}
        failed = []
        for verdict in outcome.verdicts:
            if verdict.passed or verdict.criterion_id not in criteria:
                continue
            number, row = criteria[verdict.criterion_id]
            detail = verdict.detail or {}
            argv = row.spec.get("argv") if row.kind == "command" else None
            failed.append(
                FailedCheck(
                    number,
                    detail.get("exit_code"),
                    argv,
                    str(detail.get("output_tail") or ""),
                )
            )
        return failed

    async def _run_one(
        self, run: EvalRun, strategy: Strategy, plan: TaskPlan, repeat: int
    ) -> None:
        task, config = plan.task, plan.config
        strategy_plan = plan.strategies[strategy]
        if strategy_plan.skip is not None:
            if repeat == 0:
                add_eval_result(
                    self.conn,
                    run.id,
                    task.id,
                    repeat_index=0,
                    status="skipped",
                    status_reason=strategy_plan.skip[:REASON_MAX_CHARS],
                    tags=list(task.tags),
                )
            return
        if not self.cap.allows(strategy_plan.estimates_usd[0]):
            return
        started = time.monotonic()
        repo = register_repo(
            self.conn,
            Path(task.repo.path),
            privacy_class=config.repo.privacy_class,
            provider_allow=config.repo.provider_allow,
        )
        db_task = create_task(
            self.conn,
            repo.id,
            f"eval {task.id} {strategy} #{repeat}",
            task.request,
            profile=config.budget.profile,
            source="eval",
            category=task.category,
            risk=task.expected_risk,
        )
        for criterion in task.criteria:
            add_criterion(self.conn, db_task.id, criterion.model_dump())
        env = env_for(config)
        context = self.context_for(config)
        hidden = hidden_paths(task)
        status: EvalResultStatus = "failed"
        reason: str | None = None
        feedback: str | None = None
        attempts = 0
        base: str | None = None
        try:
            base = await resolve_commit(
                Path(task.repo.path), task.repo.base, home=self.home, env=env
            )
        except WorktreeError as error:
            status, reason = "error", f"base_unresolved: {error}"[:REASON_MAX_CHARS]
        log.info("eval.task_started", extra={"eval_task": task.id, "strategy": strategy})
        routes = strategy_plan.routes if base is not None else []
        for step, route in enumerate(routes):
            if step > 0 and not self.cap.allows(strategy_plan.estimates_usd[step]):
                status, reason = "paused", "budget_cap"
                break
            if self._attempts and self.sleep_between_s:
                await self.sleep(self.sleep_between_s)
            self._attempts += 1
            before = task_cost_usd(self.conn, db_task.id)
            outcome = await run_patch_attempt(
                self.conn,
                self.artifacts,
                context,
                task_id=db_task.id,
                route=Route("api", route.model, route.effort),
                home=self.home,
                max_tokens=config.evals.max_tokens,
                context_files=task.context_files,
                eval_task=task,
                route_reason=route.reason,
                feedback=feedback,
                prepare=self._prepare(task, env, db_task.id),
                base=base,
            )
            attempts += 1
            self.cap.add(task_cost_usd(self.conn, db_task.id) - before)
            if outcome.attempt_id is not None:
                await self._remove(task, outcome.attempt_id, env)
            if outcome.task_status == "awaiting_review":
                status, reason = "solved", None
                break
            kind = escalation_kind(outcome)
            if kind is None:
                status = "paused" if outcome.attempt_status == "paused" else "error"
                reason = outcome.failure_reason or outcome.attempt_status or outcome.task_status
                break
            status, reason = "failed", kind
            feedback = failure_report(
                attempts, kind, self._failed_checks(db_task.id, outcome), hidden
            )
        counterfactual = counterfactual_usd(
            config,
            [s for a in list_task_attempts(self.conn, db_task.id) for s in list_steps(self.conn, a.id)],
        )
        set_counterfactual(self.conn, db_task.id, counterfactual)
        add_eval_result(
            self.conn,
            run.id,
            task.id,
            repeat_index=repeat,
            status=status,
            task_id=db_task.id,
            cost_usd=task_cost_usd(self.conn, db_task.id),
            attempts=attempts,
            duration_s=round(time.monotonic() - started, 3),
            escalations=max(attempts - 1, 0),
            estimate_usd=round_usd(Decimal(repr(strategy_plan.estimate_usd))),
            counterfactual_usd=counterfactual,
            status_reason=reason,
            tags=list(task.tags),
        )
        self._results += 1
        log.info(
            "eval.task_finished",
            extra={"eval_task": task.id, "strategy": strategy, "status": status},
        )
