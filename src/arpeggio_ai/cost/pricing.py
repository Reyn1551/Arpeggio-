"""Pricing engine: price windows, cost per call and token estimates (CST-02, CST-11, CFG-05).

Every function here is pure. The call time is a parameter, never read from a clock, so the
rules can be property-tested. Costs are computed exactly in ``Decimal`` from the configured
float prices and rounded to 8 decimal places only when stored (``round_usd``).

The price window is the one in effect when the request starts. A call that starts at 03:59
UTC on a weekday is billed at DeepSeek's peak price even if it finishes after 04:00.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Literal

from arpeggio_ai.config.models import (
    ModelSpec,
    PricingWindows,
    Provider,
    is_loopback_url,
    peak_window_bounds,
)

Window = Literal["peak", "offpeak", "flat"]

CHARS_PER_TOKEN = 4
_PER_MILLION = Decimal(1_000_000)
_USD_PLACES = Decimal("1e-8")


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """The prices that applied to one call, as stored on its step (CST-01)."""

    window: Window
    multiplier: float
    in_per_m: float
    cache_hit_per_m: float
    out_per_m: float
    free: bool


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts for one call. ``input_tokens`` includes ``cache_hit_tokens``."""

    input_tokens: int
    cache_hit_tokens: int
    output_tokens: int
    estimated: bool = False
    clamped: bool = False


def price_window(windows: PricingWindows | None, at: datetime) -> Window:
    """``flat`` without peak windows, else ``peak`` or ``offpeak`` at ``at`` (UTC).

    Window starts are inclusive and ends exclusive: with ``Mon-Fri 01:00-04:00``, 01:00 is
    peak and 04:00 is off-peak.
    """
    if at.tzinfo is None:
        raise ValueError("call time must be timezone-aware")
    if windows is None or not windows.peak_utc:
        return "flat"
    utc = at.astimezone(UTC)
    day, minute = utc.weekday(), utc.hour * 60 + utc.minute
    for window in windows.peak_utc:
        first, last, start, end = peak_window_bounds(window)
        if first <= day <= last and start <= minute < end:
            return "peak"
    return "offpeak"


def snapshot(model: ModelSpec, provider: Provider, at: datetime) -> PriceSnapshot:
    """Prices for a call to ``model`` starting at ``at``.

    Free models and loopback providers cost nothing, so their snapshot carries zero prices.
    """
    window = price_window(provider.pricing_windows, at)
    multiplier = 1.0
    if window == "offpeak" and provider.pricing_windows is not None:
        multiplier = provider.pricing_windows.offpeak_multiplier or 1.0
    if model.free or is_loopback_url(provider.base_url):
        return PriceSnapshot(window, multiplier, 0.0, 0.0, 0.0, free=True)
    cache_hit = model.price_cache_hit_in_per_m
    return PriceSnapshot(
        window,
        multiplier,
        model.price_in_per_m,
        model.price_in_per_m if cache_hit is None else cache_hit,
        model.price_out_per_m,
        free=False,
    )


def _decimal(value: float) -> Decimal:
    # repr gives the shortest string that round-trips, so 0.3 becomes Decimal("0.3").
    return Decimal(repr(value))


def compute_cost(
    price: PriceSnapshot, input_tokens: int, cache_hit_tokens: int, output_tokens: int
) -> Decimal:
    """Exact USD cost of one call, or 0 for a free snapshot.

    ``multiplier * ((input - cache_hit) * in + cache_hit * cache_hit_price + output * out)
    / 1_000_000``.
    """
    if min(input_tokens, cache_hit_tokens, output_tokens) < 0:
        raise ValueError("token counts must be >= 0")
    if cache_hit_tokens > input_tokens:
        raise ValueError("cache_hit_tokens must be <= input_tokens")
    if price.free:
        return Decimal(0)
    with localcontext() as context:
        context.prec = 60
        raw = (
            (input_tokens - cache_hit_tokens) * _decimal(price.in_per_m)
            + cache_hit_tokens * _decimal(price.cache_hit_per_m)
            + output_tokens * _decimal(price.out_per_m)
        )
        return _decimal(price.multiplier) * raw / _PER_MILLION


def round_usd(cost: Decimal) -> float:
    """Round to 8 decimal places (half to even) for storage."""
    return float(cost.quantize(_USD_PLACES, rounding=ROUND_HALF_EVEN))


def estimate_tokens(chars: int, chars_per_token: int = CHARS_PER_TOKEN) -> int:
    """Rough token count for text of ``chars`` characters: ``ceil(chars / chars_per_token)``.

    The default of 4 is used to record estimated costs. The spend guard passes 2 to stay on
    the safe side for code and non-Latin text, which pack fewer characters per token.
    """
    return math.ceil(max(chars, 0) / chars_per_token)


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _raw_cache_hits(usage: dict[str, Any]) -> Any:
    if "prompt_cache_hit_tokens" in usage:  # DeepSeek
        return usage["prompt_cache_hit_tokens"]
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:  # OpenAI
        return details["cached_tokens"]
    return 0


def normalize_usage(usage: object, prompt_chars: int, output_chars: int) -> Usage:
    """Turn a provider's ``usage`` object into ``Usage``.

    Reads ``prompt_tokens`` and ``completion_tokens``, and cache hits from DeepSeek's
    ``prompt_cache_hit_tokens`` or else OpenAI's ``prompt_tokens_details.cached_tokens``.
    Without usable token counts, both are estimated from the text lengths. A cache-hit
    count that is not an integer in ``0..input_tokens`` is clamped into that range, and the
    result is marked as clamped and estimated.
    """
    if isinstance(usage, dict):
        input_tokens = _count(usage.get("prompt_tokens"))
        output_tokens = _count(usage.get("completion_tokens"))
        if input_tokens is not None and output_tokens is not None:
            raw = _raw_cache_hits(usage)
            hits = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
            hits = min(max(hits, 0), input_tokens)
            clamped = hits != raw or isinstance(raw, bool)
            return Usage(input_tokens, hits, output_tokens, estimated=clamped, clamped=clamped)
    return Usage(estimate_tokens(prompt_chars), 0, estimate_tokens(output_chars), estimated=True)
