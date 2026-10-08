import tomllib
from typing import Any

import pytest
from pydantic import ValidationError

from arpeggio_ai.config.loader import template_bytes
from arpeggio_ai.config.models import INLINE_KEY_MESSAGE, Config, is_key_reference, parse_config
from arpeggio_ai.core.errors import ConfigError, ConfigIssue


def rejected(data: dict[str, Any]) -> list[ConfigIssue]:
    with pytest.raises(ConfigError) as exc_info:
        parse_config(data)
    return exc_info.value.issues


def fields(data: dict[str, Any]) -> dict[str | None, str]:
    return {issue.field: issue.message for issue in rejected(data)}


# Template (case 1) and derived values


@pytest.mark.parametrize("name", ["free", "micro-deepseek", "standard", "pro"])
def test_every_packaged_template_validates(name: str) -> None:
    text = template_bytes(name).decode("utf-8")
    config = parse_config(tomllib.loads(text))
    expected_profile = name.split("-", 1)[0]
    assert config.budget.profile == expected_profile


def test_packaged_template_validates(config_data: dict[str, Any]) -> None:
    config = parse_config(config_data)
    assert set(config.providers) == {"anthropic", "deepseek"}
    assert set(config.models) == {"tier1.cheap", "tier2.mid", "tier3.frontier"}
    assert config.defaults.counterfactual_route.model == "tier3.frontier"


def test_tier_is_derived_from_model_key(config_data: dict[str, Any]) -> None:
    config = parse_config(config_data)
    assert {key: spec.tier for key, spec in config.models.items()} == {
        "tier1.cheap": 1,
        "tier2.mid": 2,
        "tier3.frontier": 3,
    }
    assert "tier" not in config.model_dump()["models"]["tier2.mid"]


def test_repo_settings_default_to_private_with_no_allowlist(config_data: dict[str, Any]) -> None:
    repo = parse_config(config_data).repo
    assert repo.privacy_class == "private"
    assert repo.provider_allow is None


def test_config_is_frozen(config_data: dict[str, Any]) -> None:
    config = parse_config(config_data)
    with pytest.raises(ValidationError):
        config.budget.per_task_usd = 99.0  # type: ignore[misc]


# API keys (case 2)


@pytest.mark.parametrize(
    "value",
    ["sk-test-123", "sk-ant-api03-abcdef", "env:", "keychain:", "env:1BAD", "ENV:FOO", "env:A B"],
)
def test_inline_or_malformed_api_key_is_rejected(config_data: dict[str, Any], value: str) -> None:
    config_data["providers"]["anthropic"]["api_key"] = value
    assert rejected(config_data) == [
        ConfigIssue(file="merged", field="providers.anthropic.api_key", message=INLINE_KEY_MESSAGE)
    ]


@pytest.mark.parametrize("value", ["sk-test-123", "sk-ant-api03-abcdef"])
def test_rejected_api_key_never_appears_in_error(config_data: dict[str, Any], value: str) -> None:
    config_data["providers"]["anthropic"]["api_key"] = value
    with pytest.raises(ConfigError) as exc_info:
        parse_config(config_data)

    assert value not in str(exc_info.value)
    assert value not in repr(exc_info.value.issues)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__


@pytest.mark.parametrize(
    "value", ["env:ANTHROPIC_API_KEY", "keychain:arpeggio.anthropic", "env:_x-1"]
)
def test_key_references_are_accepted(config_data: dict[str, Any], value: str) -> None:
    config_data["providers"]["anthropic"]["api_key"] = value
    assert parse_config(config_data).providers["anthropic"].api_key == value


def test_non_string_api_key_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["providers"]["anthropic"]["api_key"] = 12345
    assert "providers.anthropic.api_key" in fields(config_data)


def test_is_key_reference() -> None:
    assert is_key_reference("env:X")
    assert not is_key_reference("env:X\n")
    assert not is_key_reference(None)


# Unknown fields (case 3)


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("budget",),
        ("providers", "anthropic"),
        ("models", "tier2.mid"),
        ("defaults",),
        ("defaults", "counterfactual_route"),
        ("repo",),
    ],
)
def test_unknown_field_is_rejected_with_dotted_path(
    config_data: dict[str, Any], path: tuple[str, ...]
) -> None:
    config_data.setdefault("repo", {})
    table = config_data
    for part in path:
        table = table[part]
    table["surprise"] = 1

    assert fields(config_data) == {".".join([*path, "surprise"]): "unknown field"}


def test_missing_section_is_reported(config_data: dict[str, Any]) -> None:
    del config_data["budget"]
    assert fields(config_data) == {"budget": "required field is missing"}


# References (cases 4, 6, 11)


def test_model_with_missing_provider_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["models"]["tier1.cheap"]["provider"] = "openai"
    assert fields(config_data) == {"models.tier1.cheap.provider": "unknown provider 'openai'"}


def test_counterfactual_effort_must_be_allowed_by_model(config_data: dict[str, Any]) -> None:
    config_data["defaults"]["counterfactual_route"]["effort"] = "low"
    assert fields(config_data) == {
        "defaults.counterfactual_route.effort": (
            "effort 'low' is not allowed for model 'tier3.frontier' (allowed: medium, high, max)"
        )
    }


def test_counterfactual_model_must_exist(config_data: dict[str, Any]) -> None:
    config_data["defaults"]["counterfactual_route"]["model"] = "tier2.missing"
    assert fields(config_data) == {
        "defaults.counterfactual_route.model": "unknown model 'tier2.missing'"
    }


def test_counterfactual_adapter_must_be_known(config_data: dict[str, Any]) -> None:
    config_data["defaults"]["counterfactual_route"]["adapter"] = "cursor"
    assert "defaults.counterfactual_route.adapter" in fields(config_data)


def test_provider_allow_with_unknown_provider_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["repo"] = {"provider_allow": ["anthropic", "openai"]}
    assert fields(config_data) == {"repo.provider_allow.1": "unknown provider 'openai'"}


def test_provider_allow_with_known_providers_is_accepted(config_data: dict[str, Any]) -> None:
    config_data["repo"] = {"privacy_class": "client", "provider_allow": ["anthropic"]}
    repo = parse_config(config_data).repo
    assert repo.privacy_class == "client"
    assert repo.provider_allow == ["anthropic"]


def test_reference_problems_are_reported_together(config_data: dict[str, Any]) -> None:
    config_data["models"]["tier1.cheap"]["provider"] = "openai"
    config_data["defaults"]["counterfactual_route"]["effort"] = "low"
    config_data["repo"] = {"provider_allow": ["nope"]}
    assert set(fields(config_data)) == {
        "models.tier1.cheap.provider",
        "defaults.counterfactual_route.effort",
        "repo.provider_allow.0",
    }


# Efforts (case 5)


@pytest.mark.parametrize(
    ("efforts", "field", "message"),
    [
        (["low", "turbo"], "models.tier2.mid.efforts.1", "Input should be"),
        ([], "models.tier2.mid.efforts", "at least 1 item"),
        (["low", "low"], "models.tier2.mid.efforts", "efforts must not repeat"),
    ],
)
def test_invalid_efforts_are_rejected(
    config_data: dict[str, Any], efforts: list[str], field: str, message: str
) -> None:
    config_data["models"]["tier2.mid"]["efforts"] = efforts
    found = fields(config_data)
    assert list(found) == [field]
    assert message in found[field]


# Budget (case 7)


@pytest.mark.parametrize(
    ("budget", "expected"),
    [
        ({"per_day_usd": 1.0}, {"budget.per_day_usd": "must be >= per_task_usd"}),
        ({"per_month_usd": 5.0}, {"budget.per_month_usd": "must be >= per_day_usd"}),
        (
            {"per_day_usd": 1.0, "per_month_usd": 0.5},
            {
                "budget.per_day_usd": "must be >= per_task_usd",
                "budget.per_month_usd": "must be >= per_day_usd",
            },
        ),
    ],
)
def test_budget_ordering_violations_are_rejected(
    config_data: dict[str, Any], budget: dict[str, float], expected: dict[str, str]
) -> None:
    config_data["budget"].update(budget)
    assert fields(config_data) == expected


def test_equal_budgets_are_accepted(config_data: dict[str, Any]) -> None:
    config_data["budget"].update(per_task_usd=5, per_day_usd=5, per_month_usd=5)
    assert parse_config(config_data).budget.per_month_usd == 5.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("per_task_usd", 0),
        ("per_task_usd", -1.0),
        ("per_task_usd", float("inf")),
        ("per_task_usd", float("nan")),
        ("overhead_alert", 0),
        ("overhead_alert", 1.5),
    ],
)
def test_out_of_range_budget_values_are_rejected(
    config_data: dict[str, Any], field: str, value: float
) -> None:
    config_data["budget"][field] = value
    assert f"budget.{field}" in fields(config_data)


def test_overhead_alert_of_one_is_accepted(config_data: dict[str, Any]) -> None:
    config_data["budget"]["overhead_alert"] = 1.0
    assert parse_config(config_data).budget.overhead_alert == 1.0


# Budget profiles (BUD-01)


def as_free_profile(data: dict[str, Any]) -> dict[str, Any]:
    data["budget"].update(
        profile="free", per_task_usd=0, per_day_usd=0, per_month_usd=0, eval_per_month_usd=0
    )
    for spec in data["models"].values():
        spec.update(free=True, price_in_per_m=0.0, price_out_per_m=0.0)
    return data


def as_micro_profile(data: dict[str, Any]) -> dict[str, Any]:
    data["budget"].update(
        profile="micro",
        per_task_usd=0.2,
        per_day_usd=0.5,
        per_month_usd=2.0,
        prepaid_balance_usd=2.0,
    )
    return data


def test_budget_defaults(config_data: dict[str, Any]) -> None:
    for name in ("reserve_usd", "overhead_alert", "max_quota_wait_s", "prepaid_balance_usd"):
        config_data["budget"].pop(name, None)
    budget = parse_config(config_data).budget
    assert (budget.profile, budget.reserve_usd, budget.overhead_alert) == ("standard", 0.1, 0.1)
    assert (budget.max_quota_wait_s, budget.prepaid_balance_usd) == (120, None)


@pytest.mark.parametrize("profile", ["free", "micro", "standard", "pro", "Pro", "cheap", None])
def test_profile_must_be_one_of_four(config_data: dict[str, Any], profile: str | None) -> None:
    if profile is None:
        del config_data["budget"]["profile"]
        assert fields(config_data) == {"budget.profile": "required field is missing"}
    elif profile in ("Pro", "cheap"):
        config_data["budget"]["profile"] = profile
        assert "budget.profile" in fields(config_data)
    else:
        if profile == "free":
            as_free_profile(config_data)
        elif profile == "micro":
            as_micro_profile(config_data)
        config_data["budget"]["profile"] = profile
        assert parse_config(config_data).budget.profile == profile


def test_free_profile_requires_zero_budgets(config_data: dict[str, Any]) -> None:
    config_data["budget"]["profile"] = "free"
    assert fields(config_data) == {
        "budget.per_task_usd": "must be 0 under profile 'free'",
        "budget.per_day_usd": "must be 0 under profile 'free'",
        "budget.per_month_usd": "must be 0 under profile 'free'",
        "budget.eval_per_month_usd": "must be 0 under profile 'free'",
    }


def test_free_profile_rejects_prepaid_balance(config_data: dict[str, Any]) -> None:
    as_free_profile(config_data)["budget"]["prepaid_balance_usd"] = 2.0
    assert fields(config_data) == {"budget.prepaid_balance_usd": "not allowed under profile 'free'"}


@pytest.mark.parametrize("profile", ["micro", "standard", "pro"])
def test_paid_profiles_require_positive_budgets(config_data: dict[str, Any], profile: str) -> None:
    as_micro_profile(config_data)["budget"].update(profile=profile, per_task_usd=0)
    assert fields(config_data) == {"budget.per_task_usd": f"must be > 0 under profile '{profile}'"}


def test_micro_profile_requires_prepaid_balance(config_data: dict[str, Any]) -> None:
    del as_micro_profile(config_data)["budget"]["prepaid_balance_usd"]
    assert fields(config_data) == {"budget.prepaid_balance_usd": "required under profile 'micro'"}


@pytest.mark.parametrize("reserve", [2.0, 3.0])
def test_micro_reserve_must_be_below_prepaid_balance(
    config_data: dict[str, Any], reserve: float
) -> None:
    as_micro_profile(config_data)["budget"]["reserve_usd"] = reserve
    assert fields(config_data) == {"budget.reserve_usd": "must be < prepaid_balance_usd"}


def test_micro_profile_accepts_valid_budget(config_data: dict[str, Any]) -> None:
    budget = parse_config(as_micro_profile(config_data)).budget
    assert (budget.prepaid_balance_usd, budget.reserve_usd) == (2.0, 0.1)


@pytest.mark.parametrize(
    ("field", "value"),
    [("prepaid_balance_usd", 0), ("reserve_usd", -0.1), ("max_quota_wait_s", -1)],
)
def test_out_of_range_new_budget_fields(
    config_data: dict[str, Any], field: str, value: float
) -> None:
    as_micro_profile(config_data)["budget"][field] = value
    assert f"budget.{field}" in fields(config_data)


def test_max_quota_wait_must_be_an_integer(config_data: dict[str, Any]) -> None:
    config_data["budget"]["max_quota_wait_s"] = 1.5
    assert "budget.max_quota_wait_s" in fields(config_data)


@pytest.mark.parametrize("value", ["2.0", True])
def test_numbers_are_not_coerced_from_strings_or_bools(
    config_data: dict[str, Any], value: object
) -> None:
    config_data["budget"]["per_task_usd"] = value
    assert "budget.per_task_usd" in fields(config_data)


# base_url (case 8)


def test_base_url_required_for_openai_compatible(config_data: dict[str, Any]) -> None:
    del config_data["providers"]["deepseek"]["base_url"]
    assert fields(config_data) == {
        "providers.deepseek.base_url": "required when kind = 'openai_compatible'"
    }


def test_base_url_allowed_for_anthropic_format_endpoints(config_data: dict[str, Any]) -> None:
    config_data["providers"]["anthropic"]["base_url"] = "https://api.deepseek.com/anthropic"
    config = parse_config(config_data)
    assert config.providers["anthropic"].base_url == "https://api.deepseek.com/anthropic"


HTTPS_MESSAGE = "must be an https:// URL (http:// only for localhost, 127.0.0.1 or [::1])"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com",
        "http://192.168.1.10:11434/v1",
        "http://localhost.example.com",
        "ftp://localhost",
        "api.deepseek.com",
        "https://",
        "",
        "https://api.deepseek .com",
        "http://[::1",
    ],
)
def test_base_url_must_be_https_or_loopback_http(config_data: dict[str, Any], url: str) -> None:
    config_data["providers"]["deepseek"]["base_url"] = url
    assert fields(config_data) == {"providers.deepseek.base_url": HTTPS_MESSAGE}


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:4000",
        "http://[::1]:8080/v1",
        "HTTP://LOCALHOST",
    ],
)
def test_loopback_http_is_accepted_without_api_key(config_data: dict[str, Any], url: str) -> None:
    config_data["providers"]["local"] = {"kind": "openai_compatible", "base_url": url}
    provider = parse_config(config_data).providers["local"]
    assert (provider.base_url, provider.api_key) == (url, None)


@pytest.mark.parametrize("url", ["https://api.groq.com/openai/v1", None])
def test_api_key_required_for_non_loopback(config_data: dict[str, Any], url: str | None) -> None:
    provider: dict[str, Any] = {"kind": "openai_compatible" if url else "anthropic"}
    if url:
        provider["base_url"] = url
    config_data["providers"] = {"only": provider}
    for spec in config_data["models"].values():
        spec["provider"] = "only"
    assert fields(config_data) == {
        "providers.only.api_key": "required unless base_url is a loopback address (localhost)"
    }


# Provider gateway, data_use and pricing windows (CFG-05, CFG-07)


def test_provider_defaults(config_data: dict[str, Any]) -> None:
    provider = parse_config(config_data).providers["deepseek"]
    assert (provider.gateway, provider.data_use, provider.pricing_windows) == (
        False,
        "unknown",
        None,
    )


@pytest.mark.parametrize("data_use", ["no_training", "may_train", "unknown"])
def test_data_use_values(config_data: dict[str, Any], data_use: str) -> None:
    config_data["providers"]["deepseek"].update(data_use=data_use, gateway=True)
    provider = parse_config(config_data).providers["deepseek"]
    assert (provider.data_use, provider.gateway) == (data_use, True)


def test_unknown_data_use_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["providers"]["deepseek"]["data_use"] = "never"
    assert "providers.deepseek.data_use" in fields(config_data)


def test_pricing_windows_round_trip(config_data: dict[str, Any]) -> None:
    windows = {
        "peak_utc": ["Mon-Fri 01:00-04:00", "Mon-Fri 06:00-10:00", "Sat 20:00-24:00"],
        "offpeak_multiplier": 0.5,
    }
    config_data["providers"]["deepseek"]["pricing_windows"] = windows
    parsed = parse_config(config_data).providers["deepseek"].pricing_windows
    assert parsed is not None
    assert (parsed.peak_utc, parsed.offpeak_multiplier) == (windows["peak_utc"], 0.5)


@pytest.mark.parametrize(
    ("window", "message"),
    [
        ("Mon-Fri 1:00-4:00", "must look like"),
        ("Monday 01:00-04:00", "must look like"),
        ("mon-fri 01:00-04:00", "must look like"),
        ("Mon-Fri 01:00-04:00 UTC", "must look like"),
        ("Fri-Mon 01:00-04:00", "day range must go forward"),
        ("Mon-Mon 01:00-04:00", "day range must go forward"),
        ("Mon 24:00-24:00", "times must be between"),
        ("Mon 10:60-11:00", "times must be between"),
        ("Mon 23:00-24:30", "times must be between"),
        ("Mon 04:00-01:00", "start must be earlier than end"),
        ("Mon 04:00-04:00", "start must be earlier than end"),
    ],
)
def test_bad_peak_windows_are_rejected(
    config_data: dict[str, Any], window: str, message: str
) -> None:
    config_data["providers"]["deepseek"]["pricing_windows"] = {
        "peak_utc": ["Mon-Fri 01:00-04:00", window],
        "offpeak_multiplier": 0.5,
    }
    found = fields(config_data)
    assert list(found) == ["providers.deepseek.pricing_windows.peak_utc.1"]
    assert message in found["providers.deepseek.pricing_windows.peak_utc.1"]


def test_offpeak_multiplier_required_with_windows(config_data: dict[str, Any]) -> None:
    config_data["providers"]["deepseek"]["pricing_windows"] = {"peak_utc": ["Mon 01:00-02:00"]}
    assert fields(config_data) == {
        "providers.deepseek.pricing_windows.offpeak_multiplier": "required when peak_utc is set"
    }


@pytest.mark.parametrize("multiplier", [0, 1.5, -0.5])
def test_offpeak_multiplier_range(config_data: dict[str, Any], multiplier: float) -> None:
    config_data["providers"]["deepseek"]["pricing_windows"] = {
        "peak_utc": ["Mon 01:00-02:00"],
        "offpeak_multiplier": multiplier,
    }
    assert "providers.deepseek.pricing_windows.offpeak_multiplier" in fields(config_data)


# Duplicate providers (QTA-04)


DUPLICATE = "duplicate provider endpoint; multiple accounts for the same provider are not supported"


@pytest.mark.parametrize(
    "url",
    ["https://api.deepseek.com", "https://api.deepseek.com/", "HTTPS://API.DeepSeek.com"],
)
def test_duplicate_provider_endpoint_is_rejected(config_data: dict[str, Any], url: str) -> None:
    config_data["providers"]["deepseek2"] = {
        "kind": "openai_compatible",
        "base_url": url,
        "api_key": "env:SECOND_DEEPSEEK_KEY",
    }
    assert fields(config_data) == {"providers.deepseek2": DUPLICATE}


def test_two_anthropic_providers_without_base_url_are_duplicates(
    config_data: dict[str, Any],
) -> None:
    config_data["providers"]["anthropic2"] = {"kind": "anthropic", "api_key": "env:OTHER"}
    assert fields(config_data) == {"providers.anthropic2": DUPLICATE}


def test_same_url_with_different_kind_is_not_a_duplicate(config_data: dict[str, Any]) -> None:
    config_data["providers"]["deepseek_anthropic"] = {
        "kind": "anthropic",
        "base_url": "https://api.deepseek.com",
        "api_key": "env:DEEPSEEK_API_KEY",
    }
    assert "deepseek_anthropic" in parse_config(config_data).providers


# Names and kinds


@pytest.mark.parametrize("name", ["Anthropic", "1provider", "my provider", ""])
def test_invalid_provider_name_is_rejected(config_data: dict[str, Any], name: str) -> None:
    config_data["providers"][name] = config_data["providers"]["anthropic"]
    found = fields(config_data)
    assert found[f"providers.{name}"].startswith("invalid name:")


@pytest.mark.parametrize("key", ["tier4.big", "tier0.x", "mid", "tier2.", "tier2.Mid"])
def test_invalid_model_key_is_rejected(config_data: dict[str, Any], key: str) -> None:
    config_data["models"][key] = config_data["models"]["tier2.mid"]
    found = fields(config_data)
    assert found[f"models.{key}"].startswith("invalid name:")


def test_at_least_one_provider_and_model_required(config_data: dict[str, Any]) -> None:
    config_data["providers"] = {}
    config_data["models"] = {}
    assert {"providers", "models"} <= set(fields(config_data))


def test_unknown_provider_kind_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["providers"]["anthropic"]["kind"] = "gemini"
    assert "providers.anthropic.kind" in fields(config_data)


def test_empty_model_id_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["models"]["tier2.mid"]["model"] = ""
    assert "models.tier2.mid.model" in fields(config_data)


def test_negative_price_is_rejected(config_data: dict[str, Any]) -> None:
    config_data["models"]["tier2.mid"]["price_out_per_m"] = -0.01
    assert "models.tier2.mid.price_out_per_m" in fields(config_data)


def test_issues_use_given_file_label(config_data: dict[str, Any]) -> None:
    del config_data["defaults"]
    with pytest.raises(ConfigError) as exc_info:
        parse_config(config_data, file="/x/config.toml")
    assert exc_info.value.issues[0].file == "/x/config.toml"
    assert str(exc_info.value) == "/x/config.toml: defaults: required field is missing"


def test_config_model_validate_still_raises_validation_error(config_data: dict[str, Any]) -> None:
    config_data["budget"]["per_task_usd"] = -1
    with pytest.raises(ValidationError):
        Config.model_validate(config_data)


def test_eval_budget_is_optional_and_not_negative(config_data: dict[str, Any]) -> None:
    del config_data["budget"]["eval_per_month_usd"]
    assert parse_config(config_data).budget.eval_per_month_usd is None
    config_data["budget"]["eval_per_month_usd"] = -1.0
    assert "budget.eval_per_month_usd" in fields(config_data)


def test_free_profile_allows_zero_or_unset_eval_budget(config_data: dict[str, Any]) -> None:
    as_free_profile(config_data)
    assert parse_config(config_data).budget.eval_per_month_usd == 0
    del config_data["budget"]["eval_per_month_usd"]
    assert parse_config(config_data).budget.eval_per_month_usd is None


def test_evals_middle_map_must_name_known_models(config_data: dict[str, Any]) -> None:
    config_data["evals"] = {"middle": {"docs": "tier1.cheap", "bugfix": "tier9.nope"}}
    assert "evals.middle.bugfix" in fields(config_data)
    config_data["evals"] = {"middle": {"docs": "tier1.cheap", "bugfix": "tier2.missing"}}
    assert fields(config_data) == {"evals.middle.bugfix": "unknown model 'tier2.missing'"}
    config_data["evals"] = {"middle": {"cooking": "tier1.cheap"}}
    assert any(key.startswith("evals.middle") for key in fields(config_data))
    config_data["evals"] = {"middle": {"docs": "tier1.cheap"}, "max_tokens": 4096}
    evals = parse_config(config_data).evals
    assert (evals.middle, evals.max_tokens) == ({"docs": "tier1.cheap"}, 4096)
