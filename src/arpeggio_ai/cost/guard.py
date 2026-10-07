"""Spend guard: runs before every model request and refuses calls that must not be sent.

Checks, in order:

1. The model's provider id is still a template placeholder such as ``<free-model-id>``.
2. Under the ``free`` profile the model is neither ``free = true`` nor on a loopback
   provider (BUD-02).
3. The worst case for this call (prompt tokens estimated as ``ceil(chars / 2)``, no cache
   hits, ``max_tokens`` output, at the current price window) is more than what is left of
   ``per_task_usd`` after ``spent_usd``. Budgets across attempts, days and months are
   M1.4 (CST-03).

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


def check_call(
    config: Config,
    model_key: str,
    *,
    prompt_chars: int,
    max_tokens: int,
    spent_usd: float,
    at: datetime,
) -> PriceSnapshot:
    """Raise ``SpendRefused`` if the call must not be sent, else return its price snapshot."""
    spec = config.models[model_key]
    provider = config.providers[spec.provider]
    if _PLACEHOLDER.fullmatch(spec.model):
        raise SpendRefused(f"model {model_key} still has a placeholder id; edit your config")
    if config.budget.profile == "free" and not (spec.free or is_loopback_url(provider.base_url)):
        raise SpendRefused(f"model {model_key} is not free; current profile is free")
    if max_tokens < 1:
        raise ValueError("max_tokens must be >= 1")

    price = snapshot(spec, provider, at)
    worst = round_usd(
        compute_cost(price, estimate_tokens(prompt_chars, GUARD_CHARS_PER_TOKEN), 0, max_tokens)
    )
    left = Decimal(repr(config.budget.per_task_usd)) - Decimal(repr(spent_usd))
    if Decimal(repr(worst)) > left:
        raise SpendRefused(
            f"model {model_key}: worst-case cost ${worst:.8f} for this call is more than the"
            f" ${max(left, Decimal(0)):.8f} left of per_task_usd"
        )
    return price
