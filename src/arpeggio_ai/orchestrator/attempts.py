"""Run one attempt through an adapter and record what happened (CST-01, RTE-11).

Each ``StepEvent`` becomes a step row. Its payload (request and response bodies) is written
as an artifact, and the row keeps only the summary and the artifact reference. A model
mismatch flags the attempt. When the adapter finishes, the attempt gets the adapter's final
status.
"""

import json
import logging
import sqlite3

from arpeggio_ai.adapters.base import (
    SUMMARY_MAX_CHARS,
    Adapter,
    AttemptResult,
    AttemptSpec,
    StepEvent,
)
from arpeggio_ai.core.ids import new_id
from arpeggio_ai.core.logs import log_context
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.repositories import (
    Step,
    append_step,
    set_attempt_status,
    set_model_mismatch,
)

log = logging.getLogger(__name__)


def record_step(
    conn: sqlite3.Connection, artifacts: ArtifactStore, spec: AttemptSpec, event: StepEvent
) -> Step:
    """Write the event's payload as an artifact, then append the step and update totals."""
    data = json.dumps(event.payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    ref = artifacts.write(spec.task_id, spec.attempt_id, f"step-{new_id()}.json", data)
    price = event.price
    step = append_step(
        conn,
        spec.attempt_id,
        event.kind,
        summary=event.summary[:SUMMARY_MAX_CHARS],
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
    )
    if event.model_mismatch:
        set_model_mismatch(conn, spec.attempt_id, True)
        log.warning(
            "adapter.model_mismatch",
            extra={"requested": spec.route.model, "actual_model": event.actual_model},
        )
    return step


async def run_attempt(
    conn: sqlite3.Connection, artifacts: ArtifactStore, adapter: Adapter, spec: AttemptSpec
) -> AttemptResult:
    """Drive ``adapter`` through ``spec``, recording every step, and set the final status.

    The attempt row must already exist (``store.repositories.create_attempt``). If the
    adapter raises, the attempt is marked ``error`` and the exception propagates.
    """
    with log_context(task_id=spec.task_id, attempt_id=spec.attempt_id):
        try:
            async for event in adapter.run(spec):
                record_step(conn, artifacts, spec, event)
            result = await adapter.result()
        except BaseException:
            set_attempt_status(conn, spec.attempt_id, "error")
            log.exception("attempt.crashed", extra={"adapter": adapter.name})
            raise
        set_attempt_status(conn, spec.attempt_id, result.status)
        log.info("attempt.finished", extra={"adapter": adapter.name, "status": result.status})
        return result
