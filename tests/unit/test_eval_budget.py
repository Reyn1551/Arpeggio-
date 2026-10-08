"""Eval budget: worst-case formula, run limit, spend cap and the peak notice (M0.6)."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fakes import MONDAY_OFFPEAK, MONDAY_PEAK
from routekit import build_config

from arpeggio_ai.cost.pricing import compute_cost, snapshot
from arpeggio_ai.evals.budget import (
    FEEDBACK_BASE_CHARS,
    FEEDBACK_PER_CHECK_CHARS,
    BudgetError,
    SpendCap,
    check_estimate,
    next_offpeak,
    peak_message,
    peak_notices,
    prompt_chars,
    run_limit,
    worst_case_usd,
)
from arpeggio_ai.evals.task import EvalTask

PEAKS = ["Mon-Fri 01:00-04:00", "Mon-Fri 06:00-10:00"]


def task() -> EvalTask:
    return EvalTask.model_validate(
        {
            "id": "t-one",
            "category": "bugfix",
            "expected_risk": "low",
            "repo": {"path": str(Path.cwd()), "base": "abc1234"},
            "request": "Fix the add function please.",
            "done_criteria": [
                {"kind": "command", "argv": ["python", "-m", "pytest"]},
                {"kind": "file_exists", "path": "x.txt"},
            ],
            "reference_diff": "fixtures/x.diff",
            "context_files": ["src/a.py"],
        }
    )


def test_worst_case_matches_the_guard_formula() -> None:
    config = build_config(peak_utc=PEAKS)
    spec = config.models["tier3.pro"]
    for at, multiplier in ((MONDAY_PEAK, 1.0), (MONDAY_OFFPEAK, 0.5)):
        got = worst_case_usd(config, "tier3.pro", chars=1001, max_tokens=8192, at=at)
        price = snapshot(spec, config.providers[spec.provider], at)
        assert price.multiplier == multiplier
        expected = compute_cost(price, 501, 0, 8192)  # ceil(1001 / 2) prompt tokens
        assert Decimal(repr(got)) == expected.quantize(Decimal("1e-8"))
    peak = worst_case_usd(config, "tier3.pro", chars=1000, max_tokens=100, at=MONDAY_PEAK)
    with_overhead = worst_case_usd(
        config,
        "tier3.pro",
        chars=1000,
        max_tokens=100,
        at=MONDAY_PEAK,
        observed_overhead_tokens=500,
    )
    assert with_overhead > peak


def test_free_models_estimate_zero() -> None:
    config = build_config(profile="free")
    key = next(iter(config.models))
    assert worst_case_usd(config, key, chars=10**6, max_tokens=8192, at=MONDAY_PEAK) == 0


def test_escalated_prompt_reserves_room_for_the_failure_report() -> None:
    first = prompt_chars(task(), 2000, escalated=False)
    later = prompt_chars(task(), 2000, escalated=True)
    assert later - first == FEEDBACK_BASE_CHARS + 2 * FEEDBACK_PER_CHECK_CHARS
    assert first > 2000 + len("Fix the add function please.")
    assert prompt_chars(task(), 0, escalated=False) == first - 2000


@pytest.mark.parametrize(
    ("max_usd", "monthly", "spent", "expected"),
    [
        (0.10, None, 0.0, 0.10),
        (None, 0.50, 0.20, 0.30),
        (0.10, 0.50, 0.45, 0.05),
        (1.00, 0.50, 0.60, 0.0),  # monthly budget already used up
        (0.0, 0.0, 0.0, 0.0),
    ],
)
def test_run_limit(
    max_usd: float | None, monthly: float | None, spent: float, expected: float
) -> None:
    assert run_limit(max_usd, monthly, spent) == pytest.approx(expected)


def test_run_limit_requires_a_budget() -> None:
    with pytest.raises(BudgetError, match="--max-usd"):
        run_limit(None, None, 0.0)


def test_check_estimate() -> None:
    check_estimate(0.1, 0.1)
    check_estimate(0.0, 0.0)
    with pytest.raises(BudgetError, match="worst-case estimate"):
        check_estimate(0.10000001, 0.1)


def test_spend_cap_is_a_strict_pre_check() -> None:
    cap = SpendCap(1.0)
    assert cap.allows(0.6)
    cap.add(0.5)
    assert cap.allows(0.5)
    assert not cap.allows(0.51)
    assert cap.stopped and not cap.allows(0.0)  # once stopped, nothing more starts


def test_next_offpeak() -> None:
    config = build_config(peak_utc=PEAKS)
    windows = config.providers["deep"].pricing_windows
    assert next_offpeak(windows, MONDAY_PEAK) == datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
    assert next_offpeak(windows, MONDAY_OFFPEAK) == MONDAY_OFFPEAK
    always = build_config(peak_utc=["Mon-Sun 00:00-24:00"]).providers["deep"].pricing_windows
    assert next_offpeak(always, MONDAY_PEAK) is None


def test_peak_notices_and_message() -> None:
    config = build_config(peak_utc=PEAKS)
    notices = peak_notices([(config, {"tier1.flash", "tier3.pro"})], MONDAY_PEAK)
    assert [(n.provider, n.offpeak_at) for n in notices] == [
        ("deep", datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    ]
    message = peak_message(notices)
    assert "2026-10-05 04:00 UTC" in message and "local" in message and "--allow-peak" in message
    assert peak_notices([(config, {"tier3.pro"})], MONDAY_OFFPEAK) == []
    assert peak_notices([(build_config(), {"tier3.pro"})], MONDAY_PEAK) == []  # no windows
