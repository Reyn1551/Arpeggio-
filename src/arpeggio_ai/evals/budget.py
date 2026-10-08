"""Eval run money: worst-case estimate, the run cap, and the peak-price warning (M0.6).

**Estimate.** Each planned attempt gets the spend guard's worst case (M0.3): prompt tokens
``ceil(chars / 2)`` plus the prompt overhead (``max(configured, observed)``), no cache
hits, ``[evals] max_tokens`` output, at the price window in effect now. Prompt characters
are the real prompt for the task built from the request, the checks and the context files
at ``base`` (sizes read with ``git cat-file -s``, no worktree), plus the system prompt. An
escalated attempt also reserves room for the failure report. A strategy's estimate for a
task is the sum over every route it may try, so the estimate is an upper bound.

**Cap.** ``limit = min(--max-usd, eval_per_month_usd - spent this UTC month on evals)``.
With neither set a real run needs ``--max-usd``. A run whose estimate exceeds the limit
does not start. While it runs, an attempt is started only if the spend so far plus that
attempt's worst case stays within the limit (stricter than "stop once the cap is reached",
so the cap is never overshot by more than estimate error).

**Peak.** When a resolved route's provider is in a peak window now, the run refuses to
start without ``--allow-peak`` and names the next off-peak start. Not scheduling: a
deliberate prompt to save money.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from arpeggio_ai.config.models import Config, PricingWindows
from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.cost.guard import GUARD_CHARS_PER_TOKEN, prompt_overhead
from arpeggio_ai.cost.pricing import compute_cost, estimate_tokens, price_window, round_usd, snapshot
from arpeggio_ai.evals.feedback import FEEDBACK_TAIL_CHARS
from arpeggio_ai.evals.task import EvalTask
from arpeggio_ai.orchestrator.patch import CONTEXT_CAP_BYTES, build_prompt, system_prompt
from arpeggio_ai.safety.worktree import WorktreeError, git
from arpeggio_ai.verify.criteria import describe

# Room for the failure report of an escalated attempt: header, footer, and per failing
# criterion its line plus the output tail.
FEEDBACK_BASE_CHARS = 400
FEEDBACK_PER_CHECK_CHARS = FEEDBACK_TAIL_CHARS + 100
OFFPEAK_SEARCH = timedelta(days=8)


class BudgetError(ArpeggioError):
    """An eval run must not start: over budget, no budget given, or peak prices."""


async def context_bytes(
    repo: Path,
    base: str,
    paths: Sequence[str],
    *,
    hidden: Collection[str],
    env: Mapping[str, str],
    home: Path,
) -> int:
    """Total size of the context files at ``base``, capped like ``read_context``.

    Hidden test paths are never looked at. Missing files count 0.
    """
    blocked = {path.casefold() for path in hidden}
    total = 0
    for path in paths:
        if path.casefold() in blocked:
            continue
        try:
            result = await git(["cat-file", "-s", f"{base}:{path}"], cwd=repo, env=env, home=home)
        except WorktreeError:
            continue
        if result.exit_code == 0 and result.text().strip().isdigit():
            total += int(result.text().strip())
    return min(total, CONTEXT_CAP_BYTES)


def prompt_chars(task: EvalTask, context_size: int, *, escalated: bool) -> int:
    """Characters of the system prompt and user message for one attempt of ``task``."""
    checks = [describe(criterion) for criterion in task.criteria]
    paths = [(path, "") for path in task.context_files]
    chars = len(system_prompt()) + len(build_prompt(task.request, checks, paths)) + context_size
    if escalated:
        chars += FEEDBACK_BASE_CHARS + FEEDBACK_PER_CHECK_CHARS * len(task.criteria)
    return chars


def worst_case_usd(
    config: Config,
    model_key: str,
    *,
    chars: int,
    max_tokens: int,
    at: datetime,
    observed_overhead_tokens: int = 0,
) -> float:
    """The spend guard's worst case for one call (M0.3)."""
    spec = config.models[model_key]
    price = snapshot(spec, config.providers[spec.provider], at)
    overhead = prompt_overhead(config, model_key, observed_overhead_tokens)
    tokens = estimate_tokens(chars, GUARD_CHARS_PER_TOKEN) + overhead
    return round_usd(compute_cost(price, tokens, 0, max_tokens))


def run_limit(max_usd: float | None, monthly_usd: float | None, spent_month_usd: float) -> float:
    """``min(--max-usd, eval_per_month_usd - spent this month)``. Raises without either."""
    limits: list[float] = []
    if max_usd is not None:
        limits.append(max_usd)
    if monthly_usd is not None:
        limits.append(max(0.0, monthly_usd - spent_month_usd))
    if not limits:
        raise BudgetError(
            "no eval budget: pass --max-usd or set [budget] eval_per_month_usd in config.toml"
        )
    return min(limits)


def check_estimate(estimate_usd: float, limit_usd: float) -> None:
    if Decimal(repr(estimate_usd)) > Decimal(repr(limit_usd)):
        raise BudgetError(
            f"worst-case estimate ${estimate_usd:.6f} is more than the ${limit_usd:.6f} this run"
            " may spend; lower --repeats, select fewer tasks or strategies (--id, --strategy),"
            " lower [evals] max_tokens, or raise --max-usd / eval_per_month_usd"
        )


@dataclass(slots=True)
class SpendCap:
    """Tracks actual spend during a run against ``limit_usd`` (strict pre-check)."""

    limit_usd: float
    spent_usd: float = 0.0
    stopped: bool = False
    refused: list[float] = field(default_factory=list)

    def allows(self, worst_usd: float) -> bool:
        if self.stopped:
            return False
        if self.spent_usd >= self.limit_usd or self.spent_usd + worst_usd > self.limit_usd:
            self.stopped = True
            self.refused.append(worst_usd)
            return False
        return True

    def add(self, cost_usd: float) -> None:
        self.spent_usd += cost_usd


def next_offpeak(windows: PricingWindows | None, at: datetime) -> datetime | None:
    """The first minute at or after ``at`` that is not peak, or None if never in 8 days."""
    moment = at.replace(second=0, microsecond=0)
    end = at + OFFPEAK_SEARCH
    while moment < end:
        if price_window(windows, moment) != "peak":
            return moment if moment >= at else at
        moment += timedelta(minutes=1)
    return None


@dataclass(frozen=True, slots=True)
class PeakNotice:
    provider: str
    offpeak_at: datetime | None


def peak_notices(configs: Sequence[tuple[Config, Collection[str]]], at: datetime) -> list[PeakNotice]:
    """Providers of the given model keys that are in a peak window at ``at``."""
    seen: dict[str, PeakNotice] = {}
    for config, keys in configs:
        for key in keys:
            name = config.models[key].provider
            windows = config.providers[name].pricing_windows
            if name not in seen and price_window(windows, at) == "peak":
                seen[name] = PeakNotice(name, next_offpeak(windows, at))
    return sorted(seen.values(), key=lambda notice: notice.provider)


def peak_message(notices: Sequence[PeakNotice]) -> str:
    parts = []
    for notice in notices:
        if notice.offpeak_at is None:
            parts.append(f"{notice.provider} is at peak prices")
            continue
        local = notice.offpeak_at.astimezone()
        parts.append(
            f"{notice.provider} is at peak prices until"
            f" {notice.offpeak_at:%Y-%m-%d %H:%M} UTC ({local:%Y-%m-%d %H:%M %Z} local)"
        )
    return "; ".join(parts) + ". Run then to pay off-peak prices, or pass --allow-peak."
