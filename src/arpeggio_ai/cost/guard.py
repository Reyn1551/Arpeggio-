"""Spend guard: runs before every model request and refuses calls that must not be sent.

Checks, in order:

1. The model's provider id is still a template placeholder such as ``<free-model-id>``.
2. Under the ``free`` profile the model is neither ``free = true`` nor on a loopback
   provider (BUD-02).
3. The worst case for this call (prompt tokens estimated as ``ceil(chars / 2)`` plus the
   prompt overhead, no cache hits, ``max_tokens`` output, at the current price window) is
   more than what is left of ``per_task_usd`` after ``spent_usd``. Budgets across
   attempts, days and months are M1.4 (CST-03).

The prompt overhead is the larger of the configured ``prompt_overhead_tokens`` (the
model's value, else the provider's) and the overhead observed on recent calls. Gateways
can add thousands of hidden input tokens, so prompt length alone is not trusted.

A refusal raises ``SpendRefused`` and no request is sent.
"""

import re
from datetime import datetime
from decimal import Decimal

from arpeggio_ai.config.models import Config, is_loopback_url
from arpeggio_ai.core.errors import SpendRefused
from arpeggio_ai.cost.pricing import (
    PriceSnapshot,
    compute_cost,
    estimate_tokens,
    round_usd,
    snapshot,
)

_PLACEHOLDER = re.compile(r"<.*>")
# Conservative on purpose: recorded estimates use 4 characters per token, the guard uses 2,
# so code or non-Latin prompts are not underestimated before money is spent.
GUARD_CHARS_PER_TOKEN = 2


def is_placeholder(provider_model: str) -> bool:
    """True for a template placeholder id such as ``<free-model-id>``."""
    return _PLACEHOLDER.fullmatch(provider_model) is not None


def prompt_overhead(config: Config, model_key: str, observed_tokens: int = 0) -> int:
    """Overhead to assume for the next call: max(configured, observed)."""
    spec = config.models[model_key]
    configured = spec.prompt_overhead_tokens
    if configured is None:
        configured = config.providers[spec.provider].prompt_overhead_tokens
    return max(configured, observed_tokens, 0)


def check_call(
    config: Config,
    model_key: str,
    *,
    prompt_chars: int,
    max_tokens: int,
    spent_usd: float,
    at: datetime,
    observed_overhead_tokens: int = 0,
) -> PriceSnapshot:
    """Raise ``SpendRefused`` if the call must not be sent, else return its price snapshot."""
    spec = config.models[model_key]
    provider = config.providers[spec.provider]
    if is_placeholder(spec.model):
        raise SpendRefused(f"model {model_key} still has a placeholder id; edit your config")
    if config.budget.profile == "free" and not (spec.free or is_loopback_url(provider.base_url)):
        raise SpendRefused(f"model {model_key} is not free; current profile is free")
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")

    price = snapshot(spec, provider, at)
    overhead = prompt_overhead(config, model_key, observed_overhead_tokens)
    prompt_tokens = estimate_tokens(prompt_chars, GUARD_CHARS_PER_TOKEN) + overhead
    worst = round_usd(compute_cost(price, prompt_tokens, 0, max_tokens))
    left = Decimal(repr(config.budget.per_task_usd)) - Decimal(repr(spent_usd))
    if Decimal(repr(worst)) > left:
        including = f" (including {overhead} tokens of prompt overhead)" if overhead else ""
        raise SpendRefused(
            f"model {model_key}: worst-case cost ${worst:.8f}{including} for this call is more"
            f" than the ${max(left, Decimal(0)):.8f} left of per_task_usd"
        )
    return price
