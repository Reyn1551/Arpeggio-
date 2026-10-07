import tomllib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arpeggio_ai.config.loader import template_bytes
from arpeggio_ai.config.models import (
    Config,
    ModelSpec,
    PricingWindows,
    Provider,
    parse_config,
    peak_window_bounds,
)
from arpeggio_ai.cost.pricing import (
    PriceSnapshot,
    Usage,
    compute_cost,
    estimate_tokens,
    normalize_usage,
    price_window,
    round_usd,
    snapshot,
)

DEEPSEEK = PricingWindows(
    peak_utc=["Mon-Fri 01:00-04:00", "Mon-Fri 06:00-10:00"], offpeak_multiplier=0.5
)
MONDAY = datetime(2026, 10, 5, tzinfo=UTC)  # a Monday


def at(day_offset: int, hhmm: str, seconds: float = 0) -> datetime:
    hours, minutes = (int(part) for part in hhmm.split(":"))
    return MONDAY + timedelta(days=day_offset, hours=hours, minutes=minutes, seconds=seconds)


@pytest.fixture
def micro() -> Config:
    return parse_config(tomllib.loads(template_bytes("micro-deepseek").decode("utf-8")))


def price(
    window: str = "flat",
    multiplier: float = 1.0,
    prices: tuple[float, float, float] = (0.30, 0.006, 1.20),
    free: bool = False,
) -> PriceSnapshot:
    return PriceSnapshot(window, multiplier, *prices, free=free)  # type: ignore[arg-type]


# Price windows


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (at(0, "00:59"), "offpeak"),
        (at(0, "01:00"), "peak"),  # start is inclusive
        (at(0, "03:59", 59.999), "peak"),
        (at(0, "04:00"), "offpeak"),  # end is exclusive
        (at(0, "05:59"), "offpeak"),
        (at(0, "06:00"), "peak"),
        (at(2, "08:30"), "peak"),
        (at(4, "09:59"), "peak"),  # Friday
        (at(4, "10:00"), "offpeak"),
        (at(5, "02:00"), "offpeak"),  # Saturday
        (at(6, "07:00"), "offpeak"),  # Sunday
        (at(0, "23:59"), "offpeak"),
    ],
)
def test_deepseek_windows(when: datetime, expected: str) -> None:
    assert price_window(DEEPSEEK, when) == expected


def test_window_uses_utc_not_local_time() -> None:
    jakarta = timezone(timedelta(hours=7))
    # 08:30 in UTC+7 is 01:30 UTC on Monday: peak.
    assert price_window(DEEPSEEK, datetime(2026, 10, 5, 8, 30, tzinfo=jakarta)) == "peak"
    # 07:30 in UTC+7 on Monday is 00:30 UTC: off-peak.
    assert price_window(DEEPSEEK, datetime(2026, 10, 5, 7, 30, tzinfo=jakarta)) == "offpeak"


def test_window_running_to_midnight() -> None:
    windows = PricingWindows(peak_utc=["Sat-Sun 20:00-24:00"], offpeak_multiplier=0.8)
    assert price_window(windows, at(5, "23:59", 59.9)) == "peak"
    assert price_window(windows, at(6, "00:00")) == "offpeak"
    assert price_window(windows, at(6, "20:00")) == "peak"


def test_single_day_window() -> None:
    windows = PricingWindows(peak_utc=["Wed 12:00-13:00"], offpeak_multiplier=0.5)
    assert price_window(windows, at(2, "12:30")) == "peak"
    assert price_window(windows, at(1, "12:30")) == "offpeak"


@pytest.mark.parametrize("windows", [None, PricingWindows(), PricingWindows(peak_utc=[])])
def test_no_peak_windows_is_flat(windows: PricingWindows | None) -> None:
    assert price_window(windows, at(0, "02:00")) == "flat"


def test_naive_call_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        price_window(DEEPSEEK, datetime(2026, 10, 5, 2, 0))


def test_peak_window_bounds() -> None:
    assert peak_window_bounds("Mon-Fri 01:00-04:00") == (0, 4, 60, 240)
    assert peak_window_bounds("Sun 20:30-24:00") == (6, 6, 1230, 1440)
    with pytest.raises(ValueError, match="invalid peak window"):
        peak_window_bounds("Fri-Mon 01:00-02:00")


# Snapshots


def test_snapshot_peak_and_offpeak(micro: Config) -> None:
    model, provider = micro.models["tier3.pro"], micro.providers["deepseek"]
    assert snapshot(model, provider, at(0, "02:00")) == price("peak", 1.0, (1.32, 0.044, 3.96))
    assert snapshot(model, provider, at(0, "12:00")) == price("offpeak", 0.5, (1.32, 0.044, 3.96))


def test_snapshot_without_windows_is_flat(config_data: dict[str, Any]) -> None:
    config = parse_config(config_data)  # standard template: no pricing windows
    model = config.models["tier2.mid"]
    snap = snapshot(model, config.providers[model.provider], at(0, "02:00"))
    assert (snap.window, snap.multiplier, snap.free) == ("flat", 1.0, False)


def test_free_model_snapshot_has_zero_prices(micro: Config) -> None:
    model = micro.models["tier1.flash"].model_copy(
        update={
            "free": True,
            "price_in_per_m": 0.0,
            "price_cache_hit_in_per_m": 0.0,
            "price_out_per_m": 0.0,
        }
    )
    snap = snapshot(model, micro.providers["deepseek"], at(0, "02:00"))
    assert snap == price("peak", 1.0, (0.0, 0.0, 0.0), free=True)


def test_loopback_provider_snapshot_is_free(micro: Config) -> None:
    local = Provider(kind="openai_compatible", base_url="http://localhost:11434/v1")
    snap = snapshot(micro.models["tier3.pro"], local, at(0, "02:00"))
    assert snap.free and (snap.in_per_m, snap.cache_hit_per_m, snap.out_per_m) == (0, 0, 0)


def test_snapshot_cache_hit_price_defaults_to_input_price(
    config_data: dict[str, Any],
) -> None:
    model = ModelSpec.model_validate(
        {
            "provider": "deepseek",
            "model": "m",
            "efforts": ["low"],
            "price_in_per_m": 2.0,
            "price_out_per_m": 3.0,
            "last_verified": "2026-10-01",
        }
    )
    provider = Provider(kind="openai_compatible", base_url="https://x.example", api_key="env:K")
    assert snapshot(model, provider, at(0, "02:00")).cache_hit_per_m == 2.0


# Cost


def test_flat_cost() -> None:
    # 1M input, none cached, 1M output at $0.30/$1.20 per million.
    assert compute_cost(price(), 1_000_000, 0, 1_000_000) == Decimal("1.5")


def test_peak_and_offpeak_cost() -> None:
    peak = compute_cost(price("peak"), 10_000, 2_000, 1_000)
    offpeak = compute_cost(price("offpeak", 0.5), 10_000, 2_000, 1_000)
    # 8000 * 0.30 + 2000 * 0.006 + 1000 * 1.20 = 2400 + 12 + 1200 = 3612 per million.
    assert peak == Decimal("0.003612")
    assert offpeak == Decimal("0.001806")


def test_all_cache_hits_and_no_cache_hits() -> None:
    assert compute_cost(price(), 1_000_000, 1_000_000, 0) == Decimal("0.006")
    assert compute_cost(price(), 1_000_000, 0, 0) == Decimal("0.3")


def test_free_snapshot_costs_nothing_even_with_tokens() -> None:
    assert compute_cost(price(free=True), 5_000, 1_000, 5_000) == 0


@pytest.mark.parametrize(
    ("args", "message"),
    [((-1, 0, 0), ">= 0"), ((1, 0, -1), ">= 0"), ((10, 11, 0), "<= input_tokens")],
)
def test_invalid_token_counts_raise(args: tuple[int, int, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        compute_cost(price(), *args)


def test_rounding_to_eight_decimals() -> None:
    # 7 output tokens at $1.20/M = 0.0000084 exactly.
    assert round_usd(compute_cost(price(), 0, 0, 7)) == 0.0000084
    # 1 cache-hit token at $0.006/M = 0.000000006, rounds to 0.00000001.
    assert round_usd(compute_cost(price(), 1, 1, 0)) == 0.00000001
    assert round_usd(Decimal("0.000000005")) == 0.0  # half to even
    assert round_usd(Decimal("0.000000015")) == 0.00000002


tokens = st.integers(min_value=0, max_value=10**9)
prices = st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False)
multipliers = st.floats(min_value=1e-6, max_value=1, allow_nan=False)


@st.composite
def snapshots(draw: st.DrawFn) -> PriceSnapshot:
    in_price = draw(prices)
    hit_price = draw(st.floats(min_value=0, max_value=in_price))
    return price("peak", 1.0, (in_price, hit_price, draw(prices)))


@st.composite
def usages(draw: st.DrawFn) -> tuple[int, int, int]:
    input_tokens = draw(tokens)
    return input_tokens, draw(st.integers(0, input_tokens)), draw(tokens)


@given(snapshots(), usages())
def test_cost_is_never_negative(snap: PriceSnapshot, usage: tuple[int, int, int]) -> None:
    assert compute_cost(snap, *usage) >= 0


@given(snapshots(), usages(), st.integers(1, 10**6))
def test_cost_is_monotonic_in_each_token_count(
    snap: PriceSnapshot, usage: tuple[int, int, int], extra: int
) -> None:
    input_tokens, hits, output_tokens = usage
    base = compute_cost(snap, input_tokens, hits, output_tokens)
    assert compute_cost(snap, input_tokens + extra, hits, output_tokens) >= base
    assert compute_cost(snap, input_tokens + extra, hits + extra, output_tokens) >= base
    assert compute_cost(snap, input_tokens, hits, output_tokens + extra) >= base


@given(snapshots(), usages(), multipliers)
def test_offpeak_cost_is_peak_cost_times_multiplier(
    snap: PriceSnapshot, usage: tuple[int, int, int], multiplier: float
) -> None:
    offpeak = PriceSnapshot(
        "offpeak", multiplier, snap.in_per_m, snap.cache_hit_per_m, snap.out_per_m, free=False
    )
    with localcontext() as context:
        context.prec = 60  # as in compute_cost, so the product is exact
        expected = compute_cost(snap, *usage) * Decimal(repr(multiplier))
    assert compute_cost(offpeak, *usage) == expected


# Usage


@pytest.mark.parametrize(("chars", "expected"), [(0, 0), (1, 1), (4, 1), (5, 2), (400, 100)])
def test_estimate_tokens(chars: int, expected: int) -> None:
    assert estimate_tokens(chars) == expected


def test_deepseek_usage() -> None:
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "prompt_cache_hit_tokens": 100,
        "prompt_cache_miss_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 100},
    }
    assert normalize_usage(usage, 999, 999) == Usage(120, 100, 30)


def test_openai_usage() -> None:
    usage = {
        "prompt_tokens": 2006,
        "completion_tokens": 300,
        "total_tokens": 2306,
        "prompt_tokens_details": {"cached_tokens": 1920},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    assert normalize_usage(usage, 0, 0) == Usage(2006, 1920, 300)


def test_deepseek_field_wins_when_shapes_disagree() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 1,
        "prompt_cache_hit_tokens": 60,
        "prompt_tokens_details": {"cached_tokens": 40},
    }
    assert normalize_usage(usage, 0, 0) == Usage(100, 60, 1)


@pytest.mark.parametrize(
    "extra", [{}, {"prompt_tokens_details": None}, {"prompt_tokens_details": {}}]
)
def test_usage_without_cache_fields_has_no_cache_hits(extra: dict[str, Any]) -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 5, **extra}
    assert normalize_usage(usage, 0, 0) == Usage(10, 0, 5)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        "usage",
        {},
        {"prompt_tokens": 10},
        {"prompt_tokens": -1, "completion_tokens": 5},
        {"prompt_tokens": "10", "completion_tokens": 5},
        {"prompt_tokens": True, "completion_tokens": 5},
    ],
)
def test_missing_or_malformed_usage_is_estimated(usage: object) -> None:
    assert normalize_usage(usage, 401, 9) == Usage(101, 0, 3, estimated=True)


@pytest.mark.parametrize(("hits", "expected"), [(-5, 0), (11, 10), ("3", 0), (2.5, 0), (True, 0)])
def test_cache_hits_out_of_range_are_clamped(hits: object, expected: int) -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": hits}
    assert normalize_usage(usage, 0, 0) == Usage(10, expected, 5, estimated=True, clamped=True)
