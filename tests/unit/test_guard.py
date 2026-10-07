import tomllib
from datetime import UTC, datetime
from typing import Any

import pytest

from arpeggio_ai.config.loader import template_bytes
from arpeggio_ai.config.models import Config, parse_config
from arpeggio_ai.core.errors import SpendRefused
from arpeggio_ai.cost.guard import check_call
from arpeggio_ai.cost.pricing import PriceSnapshot

PEAK = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)  # Monday 02:00 UTC
OFFPEAK = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def template(name: str) -> dict[str, Any]:
    return tomllib.loads(template_bytes(name).decode("utf-8"))


@pytest.fixture
def micro() -> Config:
    return parse_config(template("micro-deepseek"))


def check(config: Config, key: str, **kwargs: Any) -> PriceSnapshot:
    args: dict[str, Any] = {"prompt_chars": 400, "max_tokens": 100, "spent_usd": 0.0, "at": PEAK}
    return check_call(config, key, **(args | kwargs))


def as_free_profile(config: Config) -> Config:
    # Validation already rejects paid remote models under "free". The guard checks again
    # in case a Config reaches it some other way, so build one without validation.
    budget = config.budget.model_copy(
        update={"profile": "free", "per_task_usd": 0.0, "per_day_usd": 0.0, "per_month_usd": 0.0}
    )
    return config.model_copy(update={"budget": budget})


# Placeholders


def test_placeholder_model_id_is_refused() -> None:
    config = parse_config(template("free"))
    with pytest.raises(SpendRefused) as caught:
        check(config, "tier2.default")
    assert str(caught.value) == "model tier2.default still has a placeholder id; edit your config"


@pytest.mark.parametrize("model_id", ["<x>", "<your model here>"])
def test_any_angle_bracketed_id_is_a_placeholder(micro: Config, model_id: str) -> None:
    spec = micro.models["tier1.flash"].model_copy(update={"model": model_id})
    config = micro.model_copy(update={"models": {**micro.models, "tier1.flash": spec}})
    with pytest.raises(SpendRefused, match="placeholder"):
        check(config, "tier1.flash")


@pytest.mark.parametrize("model_id", ["deepseek-flash", "a<b>", "<partial", "partial>"])
def test_ordinary_ids_are_not_placeholders(micro: Config, model_id: str) -> None:
    spec = micro.models["tier1.flash"].model_copy(update={"model": model_id})
    config = micro.model_copy(update={"models": {**micro.models, "tier1.flash": spec}})
    check(config, "tier1.flash")


# Free profile (BUD-02)


def test_free_profile_refuses_paid_remote_model(micro: Config) -> None:
    with pytest.raises(SpendRefused) as caught:
        check(as_free_profile(micro), "tier3.pro")
    assert str(caught.value) == "model tier3.pro is not free; current profile is free"


def test_free_profile_allows_free_models() -> None:
    data = template("free")
    data["models"]["tier1.fast"]["model"] = "some-free-model"
    price = check(parse_config(data), "tier1.fast")
    assert price.free is True


def test_free_profile_allows_loopback_models() -> None:
    data = template("free")
    data["providers"]["ollama"] = {
        "kind": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "data_use": "no_training",
    }
    data["models"]["tier1.local"] = {
        "provider": "ollama",
        "model": "qwen3:8b",
        "efforts": ["low"],
        "price_in_per_m": 0,
        "price_out_per_m": 0,
        "last_verified": "2026-10-01",
    }
    check(parse_config(data), "tier1.local", max_tokens=10_000_000)


# Worst case


def test_worst_case_within_budget_passes(micro: Config) -> None:
    price = check(micro, "tier3.pro", prompt_chars=4000, max_tokens=1000)
    assert price.window == "peak"


def boundary_tokens() -> int:
    # tier3.pro peak: out $3.96/M. With no prompt, max_tokens alone sets the worst case.
    # per_task_usd 0.20 / 3.96 per M = 50505.05 tokens, so 50505 fits and 50506 does not.
    return 50505


def test_worst_case_exactly_at_the_budget_passes(micro: Config) -> None:
    budget = micro.budget.model_copy(update={"per_task_usd": 0.0396})
    config = micro.model_copy(update={"budget": budget})
    # 10_000 output tokens at $3.96/M = $0.0396 exactly: equal is allowed.
    check(config, "tier3.pro", prompt_chars=0, max_tokens=10_000)
    with pytest.raises(SpendRefused):
        check(config, "tier3.pro", prompt_chars=0, max_tokens=10_001)


def test_worst_case_over_budget_is_refused(micro: Config) -> None:
    check(micro, "tier3.pro", prompt_chars=0, max_tokens=boundary_tokens())
    with pytest.raises(SpendRefused) as caught:
        check(micro, "tier3.pro", prompt_chars=0, max_tokens=boundary_tokens() + 1)
    assert str(caught.value) == (
        "model tier3.pro: worst-case cost $0.20000376 for this call is more than the"
        " $0.20000000 left of per_task_usd"
    )


def test_offpeak_window_halves_the_worst_case(micro: Config) -> None:
    tokens = boundary_tokens() * 2
    with pytest.raises(SpendRefused):
        check(micro, "tier3.pro", prompt_chars=0, max_tokens=tokens)
    check(micro, "tier3.pro", prompt_chars=0, max_tokens=tokens, at=OFFPEAK)


def test_prompt_counts_toward_the_worst_case(micro: Config) -> None:
    # 1M chars -> 500k guard tokens at $1.32/M = $0.66, over the $0.20 budget on its own.
    with pytest.raises(SpendRefused):
        check(micro, "tier3.pro", prompt_chars=1_000_000, max_tokens=1)


def test_earlier_spend_shrinks_what_is_left(micro: Config) -> None:
    check(micro, "tier3.pro", prompt_chars=0, max_tokens=1000, spent_usd=0.19)
    with pytest.raises(SpendRefused, match=r"\$0\.00000000 left"):
        check(micro, "tier3.pro", prompt_chars=0, max_tokens=1000, spent_usd=0.25)


def test_max_tokens_must_be_positive(micro: Config) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        check(micro, "tier3.pro", max_tokens=0)


def test_guard_counts_two_characters_per_prompt_token(micro: Config) -> None:
    # tier3.pro peak: 4 prompt tokens at $1.32/M plus 1 output token at $3.96/M = $0.00000924.
    budget = micro.budget.model_copy(update={"per_task_usd": 0.00000924})
    config = micro.model_copy(update={"budget": budget})
    check(config, "tier3.pro", prompt_chars=8, max_tokens=1)  # ceil(8 / 2) = 4 tokens
    # ceil(9 / 2) = 5 tokens. The recording estimate, ceil(9 / 4) = 3, would have passed.
    with pytest.raises(SpendRefused, match=r"worst-case cost \$0\.00001056"):
        check(config, "tier3.pro", prompt_chars=9, max_tokens=1)
