"""Run attempts and record what happened (CST-01, RTE-11, EXE-04, EXE-08, VER-01).

Each ``StepEvent`` becomes a step row. Its payload (request and response bodies) is written
as an artifact, and the row keeps only the summary and the artifact reference. A model
mismatch flags the attempt.

``run_attempt`` drives one adapter and sets the attempt's final status.
``run_patch_attempt`` runs a whole single-shot patch attempt (ADR-0008): worktree, model
call, patch, checks, verdicts, and the final attempt and task status. The task is judged
only by its done criteria, never by the model's reply.
"""

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from arpeggio_ai.adapters import registry
from arpeggio_ai.adapters.base import (
    SUMMARY_MAX_CHARS,
    Adapter,
    AdapterContext,
    AttemptResult,
    AttemptSpec,
    Route,
    StepEvent,
)
from arpeggio_ai.core.errors import StoreError
from arpeggio_ai.core.ids import new_id
from arpeggio_ai.core.logs import log_context
from arpeggio_ai.orchestrator.patch import (
    LineEndingPlan,
    PatchError,
    apply_patch,
    build_prompt,
    extract_patch,
    read_context,
    system_prompt,
)
from arpeggio_ai.safety.process import provider_key_names, scrubbed_env
from arpeggio_ai.safety.secret_scan import (
    SecretScanner,
    current_scanner,
    scanner_from_config,
    use_scanner,
)
from arpeggio_ai.safety.worktree import WorktreeError, create_worktree
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.repositories import (
    AttemptStatus,
    Step,
    TaskStatus,
    Verdict,
    append_step,
    create_attempt,
    get_repo,
    get_task,
    list_criteria,
    max_prompt_overhead,
    set_attempt_status,
    set_attempt_worktree,
    set_failure_reason,
    set_model_mismatch,
    set_task_status,
)
from arpeggio_ai.verify.criteria import describe as describe_criterion
from arpeggio_ai.verify.runner import run_criteria

log = logging.getLogger(__name__)

RAW_PATCH_ARTIFACT = "patch-raw.diff"  # the diff as the model wrote it
PATCH_ARTIFACT = "patch.diff"  # the diff after line-ending normalization, as applied


def overhead_history(conn: sqlite3.Connection) -> Callable[[str, str], int]:
    """``AdapterContext.prompt_overhead`` backed by the last 20 recorded steps."""

    def lookup(provider: str, model: str) -> int:
        return max_prompt_overhead(conn, provider, model)

    return lookup


def record_step(
    conn: sqlite3.Connection, artifacts: ArtifactStore, spec: AttemptSpec, event: StepEvent
) -> Step:
    """Write the event's payload as an artifact, then append the step and update totals."""
    data = json.dumps(event.payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    ref = artifacts.write(spec.task_id, spec.attempt_id, f"step-{new_id()}.json", data)
    price = event.price
    # Redact before cutting, so a cut can never leave part of a secret in the row.
    summary = current_scanner().redact(event.summary, "step:summary")
    step = append_step(
        conn,
        spec.attempt_id,
        event.kind,
        summary=summary[:SUMMARY_MAX_CHARS],
        payload_ref=ref,
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        cached_tokens=event.cached_tokens,
        price_in_per_m=None if price is None else price.in_per_m,
        price_out_per_m=None if price is None else price.out_per_m,
        cost_usd=event.cost_usd,
        cost_estimated=event.cost_estimated,
        actual_model=event.actual_model,
        price_window=None if price is None else price.window,
        price_multiplier=1.0 if price is None else price.multiplier,
        price_cache_hit_per_m=None if price is None else price.cache_hit_per_m,
        provider=event.provider,
        prompt_overhead_tokens=event.prompt_overhead_tokens,
    )
    if event.model_mismatch:
        set_model_mismatch(conn, spec.attempt_id, True)
        log.warning(
            "adapter.model_mismatch",
            extra={"requested": spec.route.model, "actual_model": event.actual_model},
        )
    return step


async def drive_adapter(
    conn: sqlite3.Connection, artifacts: ArtifactStore, adapter: Adapter, spec: AttemptSpec
) -> AttemptResult:
    """Drive ``adapter`` through ``spec`` and record every step. Leaves the status alone.

    If the adapter raises, the attempt is marked ``error`` and the exception propagates.
    """
    try:
        async for event in adapter.run(spec):
            record_step(conn, artifacts, spec, event)
        return await adapter.result()
    except BaseException:
        set_attempt_status(conn, spec.attempt_id, "error")
        log.exception("attempt.crashed", extra={"adapter": adapter.name})
        raise


async def run_attempt(
    conn: sqlite3.Connection, artifacts: ArtifactStore, adapter: Adapter, spec: AttemptSpec
) -> AttemptResult:
    """Drive ``adapter`` through ``spec``, recording every step, and set the final status.

    The attempt row must already exist (``store.repositories.create_attempt``). If the
    adapter raises, the attempt is marked ``error`` and the exception propagates.
    """
    with log_context(task_id=spec.task_id, attempt_id=spec.attempt_id):
        result = await drive_adapter(conn, artifacts, adapter, spec)
        set_attempt_status(conn, spec.attempt_id, result.status)
        log.info("attempt.finished", extra={"adapter": adapter.name, "status": result.status})
        return result


# Single-shot patch attempts (ADR-0008)


@dataclass(frozen=True, slots=True)
class PatchAttemptOutcome:
    attempt_id: str | None  # None when no attempt was started (the task has no criteria)
    attempt_status: AttemptStatus | None
    task_status: TaskStatus
    failure_reason: str | None
    verdicts: list[Verdict]


def _record_failure(
    conn: sqlite3.Connection,
    artifacts: ArtifactStore,
    *,
    task_id: str,
    attempt_id: str,
    reason: str,
    detail: str,
) -> PatchAttemptOutcome:
    """Store why an attempt failed before verification and end it: attempt error, task failed."""
    detail = current_scanner().redact(detail, "failure")  # before the summary cuts it
    ref = artifacts.write(task_id, attempt_id, f"failure-{new_id()}.txt", detail.encode("utf-8"))
    append_step(
        conn,
        attempt_id,
        "message",
        summary=f"{reason}: {detail}"[:SUMMARY_MAX_CHARS],
        payload_ref=ref,
        cost_usd=0.0,
    )
    set_failure_reason(conn, attempt_id, reason)
    set_attempt_status(conn, attempt_id, "error")
    set_task_status(conn, task_id, "failed")
    log.warning("attempt.failed", extra={"reason": reason})
    return PatchAttemptOutcome(attempt_id, "error", "failed", reason, [])


def _scan_prompt_parts(
    scanner: SecretScanner, request: str, context: Sequence[tuple[str, str]]
) -> tuple[str, list[tuple[str, str]], Counter[str]]:
    """Redact the task request and each context file on its own, so events name the file."""
    counts: Counter[str] = Counter()
    result = scanner.scan_logged(request, "task:request")
    counts.update(result.counts())
    clean: list[tuple[str, str]] = []
    for path, text in context:
        scanned = scanner.scan_logged(text, f"context:{path}")
        counts.update(scanned.counts())
        clean.append((path, scanned.text))
    return result.text, clean, counts


def _record_prompt(
    conn: sqlite3.Connection,
    artifacts: ArtifactStore,
    *,
    task_id: str,
    attempt_id: str,
    files: list[str],
    redactions: Counter[str],
) -> None:
    """A ``message`` step saying which context went out and how much of it was redacted."""
    total = sum(redactions.values())
    types = dict(sorted(redactions.items()))
    if total:
        detail = ", ".join(f"{name}: {count}" for name, count in types.items())
        redacted = f"{total} {'item' if total == 1 else 'items'} redacted ({detail})"
    else:
        redacted = "nothing redacted"
    noun = "file" if len(files) == 1 else "files"
    payload = {"context_files": files, "redacted": total, "types": types}
    data = json.dumps(payload, indent=2).encode("utf-8")
    ref = artifacts.write(task_id, attempt_id, f"prompt-{new_id()}.json", data)
    summary = f"prompt: {len(files)} context {noun}, {redacted}"
    append_step(
        conn,
        attempt_id,
        "message",
        summary=summary[:SUMMARY_MAX_CHARS],
        payload_ref=ref,
        cost_usd=0.0,
    )


def _record_patch(
    conn: sqlite3.Connection,
    artifacts: ArtifactStore,
    *,
    task_id: str,
    attempt_id: str,
    plan: LineEndingPlan,
) -> None:
    """Store the diff that is about to be applied and a ``message`` step pointing at it."""
    ref = artifacts.write(task_id, attempt_id, PATCH_ARTIFACT, plan.diff.encode("utf-8"))
    counts = Counter(plan.expected.values())
    endings = ", ".join(f"{name}: {count}" for name, count in sorted(counts.items()))
    noun = "file" if len(plan.expected) == 1 else "files"
    summary = f"patch: {len(plan.expected)} {noun} (line endings {endings or 'none'})"
    append_step(
        conn,
        attempt_id,
        "message",
        summary=summary[:SUMMARY_MAX_CHARS],
        payload_ref=ref,
        cost_usd=0.0,
    )


async def run_patch_attempt(
    conn: sqlite3.Connection,
    artifacts: ArtifactStore,
    context: AdapterContext,
    *,
    task_id: str,
    route: Route,
    home: Path,
    max_tokens: int,
    timeout_s: int = 300,
    max_steps: int = 4,
    context_files: Sequence[str] = (),
) -> PatchAttemptOutcome:
    """Run one patch attempt end to end and set the attempt and task status.

    - No criteria: the task goes to ``needs_user`` and no attempt starts (VER-01).
    - The adapter stops early (guard, provider error, timeout): the attempt keeps that
      status, and the task is ``paused`` if the attempt paused, else ``failed``.
    - A patch that is missing, ambiguous, unsafe or does not apply: attempt ``error`` with
      ``failure_reason``, task ``failed``.
    - Otherwise every criterion runs: all pass gives task ``awaiting_review``, any failure
      gives task ``failed``. The attempt is ``completed`` either way.

    The worktree is kept for inspection. ``max_steps`` covers the model call and its
    retries.
    """
    task = get_task(conn, task_id)
    if task is None:
        raise StoreError(f"unknown task {task_id}")
    repo = get_repo(conn, task.repo_id)
    if repo is None:
        raise StoreError(f"unknown repo {task.repo_id}")
    criteria = list_criteria(conn, task_id)
    if not criteria:
        set_task_status(conn, task_id, "needs_user")
        log.warning("attempt.no_criteria", extra={"task_id": task_id})
        return PatchAttemptOutcome(None, None, "needs_user", "no_criteria", [])

    config = context.config
    env = scrubbed_env(extra=config.repo.check_env, deny=provider_key_names(config))
    model = config.models.get(route.model)
    attempt = create_attempt(
        conn,
        task_id,
        adapter="api",
        model=route.model,
        effort=route.effort,
        verification=route.verification,
        route_reason={"mode": "patch"},
        provider_model=None if model is None else model.model,
    )
    set_task_status(conn, task_id, "routed")
    scanner = scanner_from_config(config, context.env)
    with log_context(task_id=task_id, attempt_id=attempt.id), use_scanner(scanner):
        try:
            tree = await create_worktree(
                Path(repo.path),
                home,
                task_id=task_id,
                attempt_id=attempt.id,
                attempt_seq=attempt.seq,
                env=env,
            )
        except WorktreeError as error:
            return _record_failure(
                conn,
                artifacts,
                task_id=task_id,
                attempt_id=attempt.id,
                reason="worktree_failed",
                detail=str(error),
            )
        set_attempt_worktree(conn, attempt.id, str(tree.path), tree.branch, tree.base_sha)
        set_task_status(conn, task_id, "running")

        checks = [describe_criterion(row.parsed()) for row in criteria]
        request, context_texts, redactions = _scan_prompt_parts(
            scanner, task.request, read_context(tree.path, context_files)
        )
        _record_prompt(
            conn,
            artifacts,
            task_id=task_id,
            attempt_id=attempt.id,
            files=[path for path, _ in context_texts],
            redactions=redactions,
        )
        prompt = build_prompt(request, checks, context_texts)
        spec = AttemptSpec(
            task_id=task_id,
            attempt_id=attempt.id,
            prompt=prompt,
            route=route,
            timeout_s=timeout_s,
            max_steps=max_steps,
            max_tokens=max_tokens,
            worktree=str(tree.path),
            context_files=list(context_files),
            system=system_prompt(),
        )
        adapter = registry.create(route.adapter, context)
        try:
            result = await drive_adapter(conn, artifacts, adapter, spec)
        except BaseException:
            set_task_status(conn, task_id, "failed")
            raise
        if result.status != "completed":
            set_attempt_status(conn, attempt.id, result.status)
            task_status: TaskStatus = "paused" if result.status == "paused" else "failed"
            set_task_status(conn, task_id, task_status)
            log.info("attempt.finished", extra={"status": result.status})
            return PatchAttemptOutcome(attempt.id, result.status, task_status, None, [])

        try:
            diff = extract_patch(result.final_message)
            artifacts.write(task_id, attempt.id, RAW_PATCH_ARTIFACT, diff.encode("utf-8"))

            def record(plan: LineEndingPlan) -> None:
                _record_patch(conn, artifacts, task_id=task_id, attempt_id=attempt.id, plan=plan)

            await apply_patch(
                diff, tree.path, attempt_id=attempt.id, env=env, home=home, on_normalized=record
            )
        except PatchError as error:
            return _record_failure(
                conn,
                artifacts,
                task_id=task_id,
                attempt_id=attempt.id,
                reason=error.reason,
                detail=error.detail,
            )

        set_task_status(conn, task_id, "verifying")
        verdicts = await run_criteria(
            conn, artifacts, task_id=task_id, attempt_id=attempt.id, worktree=tree.path, env=env
        )
        set_attempt_status(conn, attempt.id, "completed")
        passed = all(verdict.passed for verdict in verdicts)
        final: TaskStatus = "awaiting_review" if passed else "failed"
        set_task_status(conn, task_id, final)
        log.info("attempt.finished", extra={"status": "completed", "task_status": final})
        return PatchAttemptOutcome(attempt.id, "completed", final, None, verdicts)
