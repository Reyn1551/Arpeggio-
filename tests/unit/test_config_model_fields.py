"""Model pricing, limits, effort params, aliases and the free-profile model rule."""

import tomllib
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from arpeggio_ai.config.loader import template_bytes
from arpeggio_ai.config.models import parse_config
from arpeggio_ai.core.errors import ConfigError

MID = "tier2.mid"


def fields(data: dict[str, Any]) -> dict[str | None, str]:
    with pytest.raises(ConfigError) as exc_info:
        parse_config(data)
    return {issue.field: issue.message for issue in exc_info.value.issues}


@pytest.fixture
def mid(config_data: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = config_data["models"][MID]
    spec.update(price_in_per_m=3.0, price_out_per_m=15.0)
    return spec


def test_model_defaults(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    spec = parse_config(config_data).models[MID]
    assert spec.price_cache_hit_in_per_m == 3.0  # defaults to price_in_per_m
    assert (spec.effort_params, spec.free, spec.response_model_aliases) == ({}, False, [])
    assert spec.limits.model_dump() == {"rpm": None, "rpd": None, "tpm": None, "tpd": None}


def test_cache_hit_price_is_shown_in_dump(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    dumped = parse_config(config_data).model_dump(mode="json")["models"][MID]
    assert dumped["price_cache_hit_in_per_m"] == 3.0


# Cache-hit price (CFG-05)


def test_explicit_cache_hit_price(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    mid["price_cache_hit_in_per_m"] = 0.3
    assert parse_config(config_data).models[MID].price_cache_hit_in_per_m == 0.3


def test_cache_hit_price_above_input_price_is_rejected(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    mid["price_cache_hit_in_per_m"] = 3.5
    assert fields(config_data) == {
        f"models.{MID}.price_cache_hit_in_per_m": "must be <= price_in_per_m"
    }


def test_invalid_input_price_reports_only_that_field(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    mid["price_in_per_m"] = "3.0"
    assert list(fields(config_data)) == [f"models.{MID}.price_in_per_m"]


# free = true


@pytest.mark.parametrize("price", ["price_in_per_m", "price_cache_hit_in_per_m", "price_out_per_m"])
def test_free_model_must_have_zero_prices(config_data: dict[str, Any], price: str) -> None:
    spec = config_data["models"][MID]
    spec.update(free=True, price_in_per_m=0.0, price_out_per_m=0.0)
    spec[price] = 0.1
    if price == "price_cache_hit_in_per_m":
        spec["price_in_per_m"] = 0.1  # keep cache-hit <= input so only the free rule fires
        expected = {
            f"models.{MID}.price_in_per_m": "must be 0 when free = true",
            f"models.{MID}.price_cache_hit_in_per_m": "must be 0 when free = true",
        }
    else:
        expected = {f"models.{MID}.{price}": "must be 0 when free = true"}
        if price == "price_in_per_m":
            expected[f"models.{MID}.price_cache_hit_in_per_m"] = "must be 0 when free = true"
    assert fields(config_data) == expected


# Effort params (CFG-08)


def test_effort_params_round_trip(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    mid["effort_params"] = {"low": {"thinking": False}, "high": {"thinking": True, "budget": 8}}
    spec = parse_config(config_data).models[MID]
    assert spec.effort_params == {
        "low": {"thinking": False},
        "high": {"thinking": True, "budget": 8},
    }


def test_effort_params_key_outside_efforts_is_rejected(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    mid["effort_params"] = {"max": {"thinking": True}}  # tier2.mid allows low, medium, high
    assert fields(config_data) == {
        f"models.{MID}.effort_params.max": "effort is not in this model's efforts"
    }


@pytest.mark.parametrize("name", ["model", "messages", "max_tokens", "stream"])
def test_effort_params_may_not_set_request_fields(
    config_data: dict[str, Any], mid: dict[str, Any], name: str
) -> None:
    mid["effort_params"] = {"high": {"thinking": {"type": "enabled"}, name: 1}}
    assert fields(config_data) == {
        f"models.{MID}.effort_params.high.{name}": (
            "set by Arpeggio; effort_params may not override it"
        )
    }


def test_micro_deepseek_uses_documented_thinking_parameters() -> None:
    # https://api-docs.deepseek.com/guides/thinking_mode: a top-level "thinking" object
    # with type enabled/disabled, and a top-level reasoning_effort.
    config = parse_config(tomllib.loads(template_bytes("micro-deepseek").decode("utf-8")))
    params = {key: spec.effort_params for key, spec in config.models.items()}
    on = {"thinking": {"type": "enabled"}}
    assert params == {
        "tier1.flash": {"low": {"thinking": {"type": "disabled"}}},
        "tier2.flash": {
            "medium": {**on, "reasoning_effort": "low"},
            "high": {**on, "reasoning_effort": "high"},
        },
        "tier3.pro": {
            "high": {**on, "reasoning_effort": "high"},
            "max": {**on, "reasoning_effort": "max"},
        },
    }


def test_effort_params_unknown_effort_is_rejected(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    mid["effort_params"] = {"turbo": {}}
    assert fields(config_data)[f"models.{MID}.effort_params.turbo"].startswith("invalid name:")


def test_effort_params_values_must_be_tables(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    mid["effort_params"] = {"low": True}
    assert f"models.{MID}.effort_params.low" in fields(config_data)


# Limits (QTA-01)


def test_limits_round_trip(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    mid["limits"] = {"rpm": 30, "rpd": 1000, "tpm": 60000, "tpd": 500000}
    limits = parse_config(config_data).models[MID].limits
    assert (limits.rpm, limits.rpd, limits.tpm, limits.tpd) == (30, 1000, 60000, 500000)


@pytest.mark.parametrize(
    ("limits", "field"),
    [
        ({"rpm": 0}, "rpm"),
        ({"rpd": -5}, "rpd"),
        ({"tpm": 1.5}, "tpm"),
        ({"tpd": True}, "tpd"),
        ({"rph": 10}, "rph"),
    ],
)
def test_bad_limits_are_rejected(
    config_data: dict[str, Any], mid: dict[str, Any], limits: dict[str, Any], field: str
) -> None:
    mid["limits"] = limits
    assert f"models.{MID}.limits.{field}" in fields(config_data)


# last_verified (CFG-09)


@pytest.mark.parametrize("value", ["2026-10-07", date(2026, 10, 7), date.today()])
def test_last_verified_accepts_dates(
    config_data: dict[str, Any], mid: dict[str, Any], value: Any
) -> None:
    mid["last_verified"] = value
    expected = value if isinstance(value, date) else date(2026, 10, 7)
    assert parse_config(config_data).models[MID].last_verified == expected


def test_last_verified_is_required(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    del mid["last_verified"]
    assert fields(config_data) == {f"models.{MID}.last_verified": "required field is missing"}


@pytest.mark.parametrize("value", ["2026-13-01", "07-10-2026", "2026/10/07", "yesterday", ""])
def test_malformed_last_verified_is_rejected(
    config_data: dict[str, Any], mid: dict[str, Any], value: str
) -> None:
    mid["last_verified"] = value
    assert fields(config_data) == {f"models.{MID}.last_verified": "must be a date like 2026-10-07"}


def test_last_verified_rejects_datetimes_and_numbers(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    mid["last_verified"] = datetime(2026, 10, 7, 12, 0)
    assert fields(config_data) == {
        f"models.{MID}.last_verified": "must be a date like 2026-10-07, without a time"
    }
    mid["last_verified"] = 20261007
    assert f"models.{MID}.last_verified" in fields(config_data)


def test_last_verified_in_the_future_is_rejected(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    mid["last_verified"] = date.today() + timedelta(days=1)
    assert fields(config_data) == {f"models.{MID}.last_verified": "must not be in the future"}


# response_model_aliases (CFG-10)


def test_aliases_round_trip(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    mid["response_model_aliases"] = ["DeepSeek-V4.1-Flash", "deepseek-flash-0813"]
    spec = parse_config(config_data).models[MID]
    assert spec.response_model_aliases == ["DeepSeek-V4.1-Flash", "deepseek-flash-0813"]


def test_empty_alias_is_rejected(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    mid["response_model_aliases"] = ["ok", ""]
    assert f"models.{MID}.response_model_aliases.1" in fields(config_data)


def test_duplicate_aliases_are_rejected(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    mid["response_model_aliases"] = ["a", "a"]
    assert fields(config_data) == {
        f"models.{MID}.response_model_aliases": "aliases must not repeat"
    }


# Free profile (BUD-02)


def free_budget(data: dict[str, Any]) -> dict[str, Any]:
    data["budget"].update(profile="free", per_task_usd=0, per_day_usd=0, per_month_usd=0)
    return data


def test_free_profile_rejects_paid_remote_models(config_data: dict[str, Any]) -> None:
    free_budget(config_data)
    for key in ("tier1.cheap", "tier3.frontier"):
        config_data["models"][key]["free"] = True
    assert fields(config_data) == {
        f"models.{MID}.free": "must be true under profile 'free' (or use a local provider)"
    }


def test_free_profile_accepts_local_models(config_data: dict[str, Any]) -> None:
    free_budget(config_data)
    config_data["providers"]["ollama"] = {
        "kind": "openai_compatible",
        "base_url": "http://localhost:11434/v1",
        "data_use": "no_training",
    }
    for spec in config_data["models"].values():
        spec["provider"] = "ollama"
    config = parse_config(config_data)
    assert all(not spec.free for spec in config.models.values())


def test_free_profile_rejects_priced_local_models(config_data: dict[str, Any]) -> None:
    free_budget(config_data)
    config_data["providers"]["ollama"] = {
        "kind": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
    }
    for spec in config_data["models"].values():
        spec["provider"] = "ollama"
    config_data["models"][MID]["price_out_per_m"] = 0.5
    assert fields(config_data) == {
        f"models.{MID}.price_out_per_m": "must be 0 under profile 'free'"
    }


# Training opt-in: global [privacy] and repo [repo] (SAF-07)


def test_opt_in_defaults(config_data: dict[str, Any]) -> None:
    config = parse_config(config_data)
    assert config.privacy.allow_training_providers is False
    assert config.repo.allow_training_providers is None
    assert config.allows_training_providers() is False


@pytest.mark.parametrize(
    ("privacy_class", "global_value", "repo_value", "expected"),
    [
        ("private", False, None, False),
        ("private", True, None, True),
        ("private", True, False, False),
        ("private", False, True, True),
        ("public", True, None, True),
        ("client", True, None, False),
        ("client", True, False, False),
        ("client", False, True, True),
    ],
)
def test_opt_in_precedence(
    config_data: dict[str, Any],
    privacy_class: str,
    global_value: bool,
    repo_value: bool | None,
    expected: bool,
) -> None:
    config_data["privacy"] = {"allow_training_providers": global_value}
    config_data["repo"] = {"privacy_class": privacy_class}
    if repo_value is not None:
        config_data["repo"]["allow_training_providers"] = repo_value
    assert parse_config(config_data).allows_training_providers() is expected


@pytest.mark.parametrize("table", ["privacy", "repo"])
def test_opt_in_must_be_boolean(config_data: dict[str, Any], table: str) -> None:
    config_data[table] = {"allow_training_providers": "yes"}
    assert f"{table}.allow_training_providers" in fields(config_data)


def test_unknown_privacy_field_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["privacy"] = {"allow_training": True}
    assert fields(config_data) == {"privacy.allow_training": "unknown field"}


def test_prompt_overhead_fields(config_data: dict[str, Any], mid: dict[str, Any]) -> None:
    config_data["providers"]["deepseek"]["prompt_overhead_tokens"] = 13_500
    mid["prompt_overhead_tokens"] = 0
    config = parse_config(config_data)
    assert config.providers["deepseek"].prompt_overhead_tokens == 13_500
    assert config.models[MID].prompt_overhead_tokens == 0
    assert config.providers["anthropic"].prompt_overhead_tokens == 0
    assert config.models["tier1.cheap"].prompt_overhead_tokens is None


def test_negative_prompt_overhead_is_rejected(
    config_data: dict[str, Any], mid: dict[str, Any]
) -> None:
    config_data["providers"]["deepseek"]["prompt_overhead_tokens"] = -1
    mid["prompt_overhead_tokens"] = -5
    problems = fields(config_data)
    assert set(problems) == {
        "providers.deepseek.prompt_overhead_tokens",
        f"models.{MID}.prompt_overhead_tokens",
    }
