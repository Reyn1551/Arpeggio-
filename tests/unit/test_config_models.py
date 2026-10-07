from typing import Any

import pytest
from pydantic import ValidationError

from arpeggio_ai.config.models import INLINE_KEY_MESSAGE, Config, is_key_reference, parse_config
from arpeggio_ai.core.errors import ConfigError, ConfigIssue


def rejected(data: dict[str, Any]) -> list[ConfigIssue]:
    with pytest.raises(ConfigError) as exc_info:
        parse_config(data)
    return exc_info.value.issues


def fields(data: dict[str, Any]) -> dict[str | None, str]:
    return {issue.field: issue.message for issue in rejected(data)}


# Template (case 1) and derived values


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


def test_base_url_forbidden_for_anthropic(config_data: dict[str, Any]) -> None:
    config_data["providers"]["anthropic"]["base_url"] = "https://api.anthropic.com"
    assert fields(config_data) == {
        "providers.anthropic.base_url": "not allowed when kind = 'anthropic'"
    }


@pytest.mark.parametrize("url", ["http://api.deepseek.com", "api.deepseek.com", "https://", ""])
def test_base_url_must_be_https(config_data: dict[str, Any], url: str) -> None:
    config_data["providers"]["deepseek"]["base_url"] = url
    assert fields(config_data) == {
        "providers.deepseek.base_url": "must be a URL starting with https://"
    }


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
