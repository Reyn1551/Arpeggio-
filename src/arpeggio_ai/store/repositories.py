"""Thin repository layer: repos, tasks, attempts, steps, criteria, verdicts and eval runs.

Records are frozen dataclasses. JSON columns are encoded and decoded here. Every write runs
in its own transaction (see ``store.db.transaction``). Status values are checked in Python
rather than with SQL CHECK constraints, so new lifecycle states only need a code change.
"""

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from arpeggio_ai.config.models import AdapterName, BudgetProfile, Effort, PrivacyClass
from arpeggio_ai.core.clock import is_utc_timestamp, utc_now
from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.core.ids import new_id
from arpeggio_ai.store.db import transaction
from arpeggio_ai.verify.criteria import (
    CommandCriterion,
    FileExistsCriterion,
    parse_criterion,
)

TaskStatus = Literal[
    "intake",
    "needs_user",
    "routed",
    "running",
    "verifying",
    "paused",
    "awaiting_review",
    "merged",
    "failed",
    "rejected",
    "cancelled",
]
TaskSource = Literal["user", "eval", "shadow"]
AttemptStatus = Literal["running", "completed", "timeout", "error", "paused", "cancelled"]
AttemptMode = Literal["normal", "explore", "shadow", "tournament"]
Verification = Literal["light", "full"]
StepKind = Literal["model_call", "tool_call", "tool_result", "message", "approval_request"]
# Rows created before migration 0002 read profile 'unknown'. New rows never get it.
StoredProfile = Literal["free", "micro", "standard", "pro", "unknown"]
PriceWindow = Literal["peak", "offpeak", "flat"]
CriterionOrigin = Literal["user", "derived"]
VerdictKind = Literal["check", "full_suite", "model_review", "user_review"]
Risk = Literal["low", "medium", "high"]
EvalRunStatus = Literal["running", "completed", "partial", "aborted"]
EvalResultStatus = Literal["solved", "failed", "error", "paused", "skipped"]
EvalSplit = Literal["tuning", "holdout", "all"]

FINAL_TASK_STATUSES = frozenset({"merged", "failed", "rejected", "cancelled"})
FINAL_ATTEMPT_STATUSES = frozenset({"completed", "timeout", "error", "cancelled"})


@dataclass(frozen=True, slots=True)
class Repo:
    id: str
    path: str
    name: str
    privacy_class: PrivacyClass
    provider_allow: list[str] | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    repo_id: str
    parent_id: str | None
    title: str
    request: str
    spec: str | None
    category: str | None
    risk: str | None
    risk_signals: dict[str, Any] | None
    status: TaskStatus
    source: TaskSource
    budget_usd: float | None
    counterfactual_usd: float | None
    created_at: str
    finished_at: str | None
    profile: StoredProfile
    is_deferrable: bool


@dataclass(frozen=True, slots=True)
class Attempt:
    id: str
    task_id: str
    seq: int
    adapter: AdapterName
    model: str
    provider_model: str | None
    effort: Effort
    verification: Verification
    route_reason: dict[str, Any]
    mode: AttemptMode
    worktree: str | None
    branch: str | None
    status: AttemptStatus
    cost_usd: float
    cost_estimated: bool
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    steps_count: int
    started_at: str
    finished_at: str | None
    checkpoint: dict[str, Any] | None
    model_mismatch: bool
    deferred_until: str | None
    base_sha: str | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    task_id: str
    kind: str
    spec: dict[str, Any]
    origin: CriterionOrigin

    def parsed(self) -> "CommandCriterion | FileExistsCriterion":
        return parse_criterion(self.spec)


@dataclass(frozen=True, slots=True)
class Verdict:
    id: str
    attempt_id: str
    criterion_id: str | None
    kind: VerdictKind
    passed: bool
    detail: dict[str, Any] | None
    log_ref: str | None
    cost_usd: float
    created_at: str


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    attempt_id: str
    seq: int
    kind: StepKind
    summary: str | None
    payload_ref: str | None
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    price_in_per_m: float | None
    price_out_per_m: float | None
    cost_usd: float | None
    cost_estimated: bool
    created_at: str
    actual_model: str | None
    price_window: PriceWindow | None
    price_multiplier: float
    price_cache_hit_per_m: float | None
    provider: str | None
    prompt_overhead_tokens: int | None
    finish_reason: str | None
    reasoning_tokens: int | None


def _require(field: str, value: str, allowed: Any) -> None:
    if value not in get_args(allowed):
        raise StoreError(f"invalid {field}: {value!r}")


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _row(
    row: sqlite3.Row, json_cols: tuple[str, ...] = (), bool_cols: tuple[str, ...] = ()
) -> dict[str, Any]:
    data = dict(row)
    for col in json_cols:
        data[col] = None if data[col] is None else json.loads(data[col])
    for col in bool_cols:
        data[col] = bool(data[col])
    return data


def _repo(row: sqlite3.Row) -> Repo:
    return Repo(**_row(row, json_cols=("provider_allow",)))


def _task(row: sqlite3.Row) -> Task:
    return Task(**_row(row, json_cols=("risk_signals",), bool_cols=("is_deferrable",)))


def _attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(
        **_row(
            row,
            json_cols=("route_reason", "checkpoint"),
            bool_cols=("cost_estimated", "model_mismatch"),
        )
    )


def _criterion(row: sqlite3.Row) -> Criterion:
    return Criterion(**_row(row, json_cols=("spec",)))


def _verdict(row: sqlite3.Row) -> Verdict:
    return Verdict(**_row(row, json_cols=("detail",), bool_cols=("passed",)))


def _step(row: sqlite3.Row) -> Step:
    return Step(**_row(row, bool_cols=("cost_estimated",)))


def _next_seq(conn: sqlite3.Connection, table: str, parent_col: str, parent_id: str) -> int:
    # Only called inside a write transaction, so no other writer can take the same seq.
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} WHERE {parent_col} = ?", (parent_id,)
    ).fetchone()
    return int(row[0])


# Repos


def ensure_repo(
    conn: sqlite3.Connection,
    path: str,
    name: str,
    privacy_class: PrivacyClass = "private",
    provider_allow: list[str] | None = None,
) -> Repo:
    """Insert the repo, or update its name and privacy settings to match the current config."""
    _require("privacy_class", privacy_class, PrivacyClass)
    with transaction(conn):
        conn.execute(
            "INSERT INTO repos (id, path, name, privacy_class, provider_allow, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (path) DO UPDATE SET name = excluded.name,"
            " privacy_class = excluded.privacy_class, provider_allow = excluded.provider_allow",
            (new_id(), path, name, privacy_class, _dumps(provider_allow), utc_now()),
        )
        row = conn.execute("SELECT * FROM repos WHERE path = ?", (path,)).fetchone()
    return _repo(row)


def register_repo(
    conn: sqlite3.Connection,
    path: Path,
    name: str | None = None,
    privacy_class: PrivacyClass = "private",
    provider_allow: list[str] | None = None,
) -> Repo:
    """Register a repository by its absolute, resolved path. Idempotent."""
    if not path.is_absolute():
        raise StoreError(f"repo path must be absolute: {path}")
    resolved = path.resolve()
    return ensure_repo(conn, str(resolved), name or resolved.name, privacy_class, provider_allow)


def get_repo(conn: sqlite3.Connection, repo_id: str) -> Repo | None:
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return None if row is None else _repo(row)


# Tasks


def create_task(
    conn: sqlite3.Connection,
    repo_id: str,
    title: str,
    request: str,
    *,
    profile: BudgetProfile,
    is_deferrable: bool = False,
    status: TaskStatus = "intake",
    source: TaskSource = "user",
    parent_id: str | None = None,
    category: str | None = None,
    budget_usd: float | None = None,
    risk: Risk | None = None,
) -> Task:
    """Create a task. ``profile`` is the budget profile it runs under (BUD-01).

    Only the four real profiles are accepted. ``unknown`` exists only on rows created
    before migration 0002.
    """
    _require("task profile", profile, BudgetProfile)
    _require("task status", status, TaskStatus)
    _require("task source", source, TaskSource)
    if risk is not None:
        _require("task risk", risk, Risk)
    task_id = new_id()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO tasks (id, repo_id, parent_id, title, request, category, status,"
                " source, budget_usd, created_at, profile, is_deferrable, risk)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    repo_id,
                    parent_id,
                    title,
                    request,
                    category,
                    status,
                    source,
                    budget_usd,
                    utc_now(),
                    profile,
                    int(is_deferrable),
                    risk,
                ),
            )
    except sqlite3.IntegrityError as error:
        raise StoreError(f"cannot create task (unknown repo or parent?): {error}") from error
    return _get_task(conn, task_id)


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return None if row is None else _task(row)


def _get_task(conn: sqlite3.Connection, task_id: str) -> Task:
    task = get_task(conn, task_id)
    if task is None:
        raise StoreError(f"unknown task {task_id}")
    return task


def set_task_status(conn: sqlite3.Connection, task_id: str, status: TaskStatus) -> Task:
    """Change a task's status. Final statuses also set ``finished_at``."""
    _require("task status", status, TaskStatus)
    finished_at = utc_now() if status in FINAL_TASK_STATUSES else None
    with transaction(conn):
        updated = conn.execute(
            "UPDATE tasks SET status = ?, finished_at = ? WHERE id = ?",
            (status, finished_at, task_id),
        ).rowcount
    if updated == 0:
        raise StoreError(f"unknown task {task_id}")
    return _get_task(conn, task_id)


def set_counterfactual(conn: sqlite3.Connection, task_id: str, cost_usd: float) -> Task:
    """Store the task's counterfactual cost: its tokens priced on the default route (CST-05)."""
    if not math.isfinite(cost_usd) or cost_usd < 0:
        raise StoreError(f"counterfactual cost must be >= 0: {cost_usd!r}")
    with transaction(conn):
        updated = conn.execute(
            "UPDATE tasks SET counterfactual_usd = ? WHERE id = ?", (cost_usd, task_id)
        ).rowcount
    if updated == 0:
        raise StoreError(f"unknown task {task_id}")
    return _get_task(conn, task_id)


def task_cost_usd(conn: sqlite3.Connection, task_id: str) -> float:
    """Attempt costs plus verification costs of one task, failed attempts included."""
    row = conn.execute(
        "SELECT (SELECT COALESCE(SUM(cost_usd), 0) FROM attempts WHERE task_id = ?)"
        " + (SELECT COALESCE(SUM(v.cost_usd), 0) FROM verdicts v"
        " JOIN attempts a ON a.id = v.attempt_id WHERE a.task_id = ?)",
        (task_id, task_id),
    ).fetchone()
    return float(row[0])


# Attempts


def create_attempt(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    adapter: AdapterName,
    model: str,
    effort: Effort,
    verification: Verification,
    route_reason: dict[str, Any],
    mode: AttemptMode = "normal",
    provider_model: str | None = None,
    worktree: str | None = None,
    branch: str | None = None,
) -> Attempt:
    """Start a new attempt with the next ``seq`` for its task and status ``running``."""
    _require("adapter", adapter, AdapterName)
    _require("effort", effort, Effort)
    _require("verification", verification, Verification)
    _require("attempt mode", mode, AttemptMode)
    attempt_id = new_id()
    with transaction(conn):
        _get_task(conn, task_id)
        seq = _next_seq(conn, "attempts", "task_id", task_id)
        conn.execute(
            "INSERT INTO attempts (id, task_id, seq, adapter, model, provider_model, effort,"
            " verification, route_reason, mode, worktree, branch, status, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
            (
                attempt_id,
                task_id,
                seq,
                adapter,
                model,
                provider_model,
                effort,
                verification,
                _dumps(route_reason),
                mode,
                worktree,
                branch,
                utc_now(),
            ),
        )
    return _get_attempt(conn, attempt_id)


def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> Attempt | None:
    row = conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    return None if row is None else _attempt(row)


def _get_attempt(conn: sqlite3.Connection, attempt_id: str) -> Attempt:
    attempt = get_attempt(conn, attempt_id)
    if attempt is None:
        raise StoreError(f"unknown attempt {attempt_id}")
    return attempt


def set_attempt_status(conn: sqlite3.Connection, attempt_id: str, status: AttemptStatus) -> Attempt:
    """Change an attempt's status. Final statuses also set ``finished_at``."""
    _require("attempt status", status, AttemptStatus)
    finished_at = utc_now() if status in FINAL_ATTEMPT_STATUSES else None
    with transaction(conn):
        updated = conn.execute(
            "UPDATE attempts SET status = ?, finished_at = ? WHERE id = ?",
            (status, finished_at, attempt_id),
        ).rowcount
    if updated == 0:
        raise StoreError(f"unknown attempt {attempt_id}")
    return _get_attempt(conn, attempt_id)


def _update_attempt(conn: sqlite3.Connection, attempt_id: str, column: str, value: Any) -> Attempt:
    with transaction(conn):
        updated = conn.execute(
            f"UPDATE attempts SET {column} = ? WHERE id = ?", (value, attempt_id)
        ).rowcount
    if updated == 0:
        raise StoreError(f"unknown attempt {attempt_id}")
    return _get_attempt(conn, attempt_id)


def set_model_mismatch(conn: sqlite3.Connection, attempt_id: str, mismatch: bool) -> Attempt:
    """Flag (or clear) that the served model differed from the requested one (RTE-11)."""
    return _update_attempt(conn, attempt_id, "model_mismatch", int(mismatch))


def set_deferred_until(conn: sqlite3.Connection, attempt_id: str, until: str | None) -> Attempt:
    """Hold an attempt until a UTC timestamp, for example the next off-peak window (CST-10)."""
    if until is not None and not is_utc_timestamp(until):
        raise StoreError(f"deferred_until must look like 2026-10-07T01:00:00.000Z: {until!r}")
    return _update_attempt(conn, attempt_id, "deferred_until", until)


def set_attempt_worktree(
    conn: sqlite3.Connection, attempt_id: str, worktree: str, branch: str, base_sha: str
) -> Attempt:
    """Record where an attempt runs: its worktree, branch and starting commit (EXE-04)."""
    with transaction(conn):
        updated = conn.execute(
            "UPDATE attempts SET worktree = ?, branch = ?, base_sha = ? WHERE id = ?",
            (worktree, branch, base_sha, attempt_id),
        ).rowcount
    if updated == 0:
        raise StoreError(f"unknown attempt {attempt_id}")
    return _get_attempt(conn, attempt_id)


def set_failure_reason(conn: sqlite3.Connection, attempt_id: str, reason: str) -> Attempt:
    """Why an attempt failed before verification, for example ``patch_unsafe``."""
    if not reason:
        raise StoreError("failure reason must not be empty")
    return _update_attempt(conn, attempt_id, "failure_reason", reason)


# Done criteria and verdicts


def add_criterion(
    conn: sqlite3.Connection,
    task_id: str,
    spec: object,
    origin: CriterionOrigin = "user",
) -> Criterion:
    """Validate ``spec`` (see verify/criteria.py) and store it for the task."""
    _require("criterion origin", origin, CriterionOrigin)
    parsed = parse_criterion(spec)
    criterion_id = new_id()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO done_criteria (id, task_id, kind, spec, origin)"
                " VALUES (?, ?, ?, ?, ?)",
                (criterion_id, task_id, parsed.kind, _dumps(parsed.model_dump()), origin),
            )
    except sqlite3.IntegrityError as error:
        raise StoreError(f"cannot add criterion (unknown task?): {error}") from error
    row = conn.execute("SELECT * FROM done_criteria WHERE id = ?", (criterion_id,)).fetchone()
    return _criterion(row)


def list_criteria(conn: sqlite3.Connection, task_id: str) -> list[Criterion]:
    """A task's criteria in the order they were added."""
    rows = conn.execute("SELECT * FROM done_criteria WHERE task_id = ? ORDER BY rowid", (task_id,))
    return [_criterion(row) for row in rows]


def add_verdict(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    kind: VerdictKind,
    passed: bool,
    criterion_id: str | None = None,
    detail: dict[str, Any] | None = None,
    log_ref: str | None = None,
    cost_usd: float = 0.0,
) -> Verdict:
    """Store the result of one check (VER-04)."""
    _require("verdict kind", kind, VerdictKind)
    if not math.isfinite(cost_usd) or cost_usd < 0:
        raise StoreError(f"cost_usd must be >= 0: {cost_usd!r}")
    verdict_id = new_id()
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO verdicts (id, attempt_id, criterion_id, kind, passed, detail,"
                " log_ref, cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verdict_id,
                    attempt_id,
                    criterion_id,
                    kind,
                    int(passed),
                    _dumps(detail),
                    log_ref,
                    cost_usd,
                    utc_now(),
                ),
            )
    except sqlite3.IntegrityError as error:
        raise StoreError(f"cannot add verdict (unknown attempt or criterion?): {error}") from error
    row = conn.execute("SELECT * FROM verdicts WHERE id = ?", (verdict_id,)).fetchone()
    return _verdict(row)


def list_verdicts(conn: sqlite3.Connection, attempt_id: str) -> list[Verdict]:
    rows = conn.execute("SELECT * FROM verdicts WHERE attempt_id = ? ORDER BY rowid", (attempt_id,))
    return [_verdict(row) for row in rows]


# Steps


def append_step(
    conn: sqlite3.Connection,
    attempt_id: str,
    kind: StepKind,
    *,
    summary: str | None = None,
    payload_ref: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    price_in_per_m: float | None = None,
    price_out_per_m: float | None = None,
    cost_usd: float | None = None,
    cost_estimated: bool = False,
    actual_model: str | None = None,
    price_window: PriceWindow | None = None,
    price_multiplier: float = 1.0,
    price_cache_hit_per_m: float | None = None,
    provider: str | None = None,
    prompt_overhead_tokens: int | None = None,
    finish_reason: str | None = None,
    reasoning_tokens: int | None = None,
) -> Step:
    """Record one step and add its tokens and cost to the attempt totals, atomically.

    The price fields are a snapshot of what applied to this call: the window (peak, off-peak
    or flat), its multiplier and the cache-hit input price. ``cost_usd`` is computed by the
    caller. ``actual_model`` is the model the provider says served the call (RTE-11).
    ``provider`` and ``prompt_overhead_tokens`` feed the spend guard (``max_prompt_overhead``).
    ``finish_reason`` and ``reasoning_tokens`` are what the provider reported for a model
    call; reasoning tokens are part of ``output_tokens``, not added to them.
    """
    _require("step kind", kind, StepKind)
    if price_window is not None:
        _require("price window", price_window, PriceWindow)
    if not math.isfinite(price_multiplier) or price_multiplier <= 0:
        raise StoreError(f"price_multiplier must be > 0: {price_multiplier!r}")
    if prompt_overhead_tokens is not None and prompt_overhead_tokens < 0:
        raise StoreError(f"prompt_overhead_tokens must be >= 0: {prompt_overhead_tokens!r}")
    if reasoning_tokens is not None and reasoning_tokens < 0:
        raise StoreError(f"reasoning_tokens must be >= 0: {reasoning_tokens!r}")
    step_id = new_id()
    with transaction(conn):
        _get_attempt(conn, attempt_id)
        seq = _next_seq(conn, "steps", "attempt_id", attempt_id)
        conn.execute(
            "INSERT INTO steps (id, attempt_id, seq, kind, summary, payload_ref, input_tokens,"
            " output_tokens, cached_tokens, price_in_per_m, price_out_per_m, cost_usd,"
            " cost_estimated, created_at, actual_model, price_window, price_multiplier,"
            " price_cache_hit_per_m, provider, prompt_overhead_tokens, finish_reason,"
            " reasoning_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                step_id,
                attempt_id,
                seq,
                kind,
                summary,
                payload_ref,
                input_tokens,
                output_tokens,
                cached_tokens,
                price_in_per_m,
                price_out_per_m,
                cost_usd,
                int(cost_estimated),
                utc_now(),
                actual_model,
                price_window,
                price_multiplier,
                price_cache_hit_per_m,
                provider,
                prompt_overhead_tokens,
                finish_reason,
                reasoning_tokens,
            ),
        )
        conn.execute(
            "UPDATE attempts SET cost_usd = cost_usd + ?, input_tokens = input_tokens + ?,"
            " output_tokens = output_tokens + ?, cached_tokens = cached_tokens + ?,"
            " steps_count = steps_count + 1, cost_estimated = MAX(cost_estimated, ?)"
            " WHERE id = ?",
            (
                cost_usd or 0.0,
                input_tokens or 0,
                output_tokens or 0,
                cached_tokens or 0,
                int(cost_estimated),
                attempt_id,
            ),
        )
        row = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
    return _step(row)


def max_prompt_overhead(
    conn: sqlite3.Connection, provider: str, model: str, *, last: int = 20
) -> int:
    """Highest prompt overhead among the ``last`` recorded steps for this provider and model.

    ``model`` is the logical model key (``attempts.model``). Returns 0 without history.
    """
    row = conn.execute(
        "SELECT MAX(prompt_overhead_tokens) FROM ("
        " SELECT s.prompt_overhead_tokens FROM steps s JOIN attempts a ON a.id = s.attempt_id"
        " WHERE s.provider = ? AND a.model = ? AND s.prompt_overhead_tokens IS NOT NULL"
        " ORDER BY s.created_at DESC, s.rowid DESC LIMIT ?)",
        (provider, model, last),
    ).fetchone()
    return int(row[0] or 0)


def list_steps(conn: sqlite3.Connection, attempt_id: str) -> list[Step]:
    rows = conn.execute("SELECT * FROM steps WHERE attempt_id = ? ORDER BY seq", (attempt_id,))
    return [_step(row) for row in rows]


# Eval runs and results (M0.6)


@dataclass(frozen=True, slots=True)
class EvalRun:
    id: str
    strategy: str
    split: EvalSplit
    git_sha: str
    config_hash: str
    started_at: str
    finished_at: str | None
    summary: dict[str, Any] | None
    profile: StoredProfile
    status: EvalRunStatus
    status_reason: str | None
    repeats: int
    planned: int | None
    estimate_usd: float | None


@dataclass(frozen=True, slots=True)
class EvalResult:
    eval_run_id: str
    eval_task: str
    task_id: str | None
    solved: bool
    cost_usd: float
    attempts: int
    duration_s: float
    quota_wait_s: float
    repeat_index: int
    status: EvalResultStatus
    status_reason: str | None
    escalations: int
    estimate_usd: float | None
    counterfactual_usd: float | None
    tags: list[str] | None


def _eval_run(row: sqlite3.Row) -> EvalRun:
    return EvalRun(**_row(row, json_cols=("summary",)))


def _eval_result(row: sqlite3.Row) -> EvalResult:
    return EvalResult(**_row(row, json_cols=("tags",), bool_cols=("solved",)))


def create_eval_run(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    split: EvalSplit,
    git_sha: str,
    config_hash: str,
    profile: BudgetProfile,
    repeats: int,
    planned: int,
    estimate_usd: float,
) -> EvalRun:
    """Start an eval run with status ``running``."""
    _require("split", split, EvalSplit)
    _require("eval run profile", profile, BudgetProfile)
    if repeats < 1:
        raise StoreError("repeats must be >= 1")
    run_id = new_id()
    with transaction(conn):
        conn.execute(
            "INSERT INTO eval_runs (id, strategy, split, git_sha, config_hash, started_at,"
            " profile, status, repeats, planned, estimate_usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
            (
                run_id,
                strategy,
                split,
                git_sha,
                config_hash,
                utc_now(),
                profile,
                repeats,
                planned,
                estimate_usd,
            ),
        )
    return _get_eval_run(conn, run_id)


def get_eval_run(conn: sqlite3.Connection, run_id: str) -> EvalRun | None:
    row = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
    return None if row is None else _eval_run(row)


def _get_eval_run(conn: sqlite3.Connection, run_id: str) -> EvalRun:
    run = get_eval_run(conn, run_id)
    if run is None:
        raise StoreError(f"unknown eval run {run_id}")
    return run


def list_eval_runs(conn: sqlite3.Connection) -> list[EvalRun]:
    """Every eval run, oldest first."""
    rows = conn.execute("SELECT * FROM eval_runs ORDER BY started_at, rowid")
    return [_eval_run(row) for row in rows]


def finish_eval_run(
    conn: sqlite3.Connection,
    run_id: str,
    status: EvalRunStatus,
    *,
    reason: str | None = None,
    summary: dict[str, Any] | None = None,
) -> EvalRun:
    """Close a run: ``completed``, ``partial`` (stopped early, with a reason) or ``aborted``."""
    _require("eval run status", status, EvalRunStatus)
    with transaction(conn):
        updated = conn.execute(
            "UPDATE eval_runs SET status = ?, status_reason = ?, summary = ?, finished_at = ?"
            " WHERE id = ?",
            (status, reason, _dumps(summary), utc_now(), run_id),
        ).rowcount
    if updated == 0:
        raise StoreError(f"unknown eval run {run_id}")
    return _get_eval_run(conn, run_id)


def add_eval_result(
    conn: sqlite3.Connection,
    run_id: str,
    eval_task: str,
    *,
    repeat_index: int,
    status: EvalResultStatus,
    task_id: str | None = None,
    cost_usd: float = 0.0,
    attempts: int = 0,
    duration_s: float = 0.0,
    escalations: int = 0,
    estimate_usd: float | None = None,
    counterfactual_usd: float | None = None,
    status_reason: str | None = None,
    tags: list[str] | None = None,
) -> EvalResult:
    """Store the outcome of one task run. ``solved`` follows from ``status``."""
    _require("eval result status", status, EvalResultStatus)
    if status != "skipped" and task_id is None:
        raise StoreError("only a skipped result may have no task")
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO eval_results (eval_run_id, eval_task, task_id, solved, cost_usd,"
                " attempts, duration_s, repeat_index, status, status_reason, escalations,"
                " estimate_usd, counterfactual_usd, tags)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    eval_task,
                    task_id,
                    int(status == "solved"),
                    cost_usd,
                    attempts,
                    duration_s,
                    repeat_index,
                    status,
                    status_reason,
                    escalations,
                    estimate_usd,
                    counterfactual_usd,
                    _dumps(tags),
                ),
            )
    except sqlite3.IntegrityError as error:
        raise StoreError(f"cannot add eval result: {error}") from error
    row = conn.execute(
        "SELECT * FROM eval_results WHERE eval_run_id = ? AND eval_task = ? AND repeat_index = ?",
        (run_id, eval_task, repeat_index),
    ).fetchone()
    return _eval_result(row)


def list_eval_results(conn: sqlite3.Connection, run_id: str) -> list[EvalResult]:
    rows = conn.execute(
        "SELECT * FROM eval_results WHERE eval_run_id = ? ORDER BY rowid", (run_id,)
    )
    return [_eval_result(row) for row in rows]


def list_task_attempts(conn: sqlite3.Connection, task_id: str) -> list[Attempt]:
    rows = conn.execute("SELECT * FROM attempts WHERE task_id = ? ORDER BY seq", (task_id,))
    return [_attempt(row) for row in rows]


_EVAL_TASKS = "SELECT task_id FROM eval_results WHERE task_id IS NOT NULL"


def month_eval_spend_usd(conn: sqlite3.Connection, month: str) -> float:
    """Attempt and verification costs of tasks linked to eval results and created in
    ``month`` (``YYYY-MM``, UTC). Tasks without an eval result never count."""
    pattern = f"{month}-%"
    attempts = conn.execute(
        "SELECT COALESCE(SUM(a.cost_usd), 0) FROM attempts a JOIN tasks t ON t.id = a.task_id"
        f" WHERE t.created_at LIKE ? AND t.id IN ({_EVAL_TASKS})",
        (pattern,),
    ).fetchone()
    verdicts = conn.execute(
        "SELECT COALESCE(SUM(v.cost_usd), 0) FROM verdicts v"
        " JOIN attempts a ON a.id = v.attempt_id JOIN tasks t ON t.id = a.task_id"
        f" WHERE t.created_at LIKE ? AND t.id IN ({_EVAL_TASKS})",
        (pattern,),
    ).fetchone()
    return float(attempts[0]) + float(verdicts[0])
