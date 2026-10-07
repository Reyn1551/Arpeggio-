"""Pydantic models for ``config.toml``.

Single-field rules use Pydantic constraints and field validators. Rules that span fields
(budget ordering, base_url per provider kind, references between providers, models and
routes) run in ``model_validator(mode="after")`` hooks and report the exact field at fault.
"""

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from arpeggio_ai.core.errors import ConfigError, ConfigIssue

Effort = Literal["low", "medium", "high", "max"]
AdapterName = Literal["claude_code", "opencode", "command_code", "api"]
ProviderKind = Literal["anthropic", "openai_compatible"]
PrivacyClass = Literal["public", "private", "client"]
BudgetProfile = Literal["free", "micro", "standard", "pro"]

ProviderName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
ModelKey = Annotated[str, StringConstraints(pattern=r"^tier[1-3]\.[a-z0-9_-]+$")]

API_KEY_REF = re.compile(r"(env|keychain):[A-Za-z_][A-Za-z0-9_.-]*")
INLINE_KEY_MESSAGE = "inline API keys are not allowed; use env:VAR or keychain:NAME"
HTTPS_URL = re.compile(r"https://\S+")

Loc = tuple[str | int, ...]


def is_key_reference(value: object) -> bool:
    """True if ``value`` is an ``env:VAR`` or ``keychain:NAME`` reference."""
    return isinstance(value, str) and API_KEY_REF.fullmatch(value) is not None


def _raise_if_any(title: str, problems: list[tuple[Loc, str]]) -> None:
    """Raise one ValidationError holding every problem, each at its own field location.

    Pydantic prefixes these locations with the outer path, so a problem at ``("base_url",)``
    inside ``providers.deepseek`` is reported as ``providers.deepseek.base_url``.
    """
    if problems:
        raise ValidationError.from_exception_data(
            title,
            [
                InitErrorDetails(
                    type=PydanticCustomError("config_rule", message), loc=loc, input=None
                )
                for loc, message in problems
            ],
        )


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class Budget(_Model):
    profile: BudgetProfile
    per_task_usd: float = Field(ge=0)
    per_day_usd: float = Field(ge=0)
    per_month_usd: float = Field(ge=0)
    prepaid_balance_usd: float | None = Field(default=None, gt=0)
    reserve_usd: float = Field(default=0.10, ge=0)
    overhead_alert: float = Field(default=0.10, gt=0, le=1)
    max_quota_wait_s: int = Field(default=120, ge=0)

    @model_validator(mode="after")
    def _check_profile_rules(self) -> Self:
        problems: list[tuple[Loc, str]] = []
        amounts = ("per_task_usd", "per_day_usd", "per_month_usd")
        if self.profile == "free":
            problems += [
                ((name,), "must be 0 under profile 'free'")
                for name in amounts
                if getattr(self, name) != 0
            ]
            if self.prepaid_balance_usd is not None:
                problems.append((("prepaid_balance_usd",), "not allowed under profile 'free'"))
        else:
            problems += [
                ((name,), f"must be > 0 under profile '{self.profile}'")
                for name in amounts
                if getattr(self, name) == 0
            ]
            if self.per_day_usd < self.per_task_usd:
                problems.append((("per_day_usd",), "must be >= per_task_usd"))
            if self.per_month_usd < self.per_day_usd:
                problems.append((("per_month_usd",), "must be >= per_day_usd"))
        if self.profile == "micro":
            if self.prepaid_balance_usd is None:
                problems.append((("prepaid_balance_usd",), "required under profile 'micro'"))
            elif self.reserve_usd >= self.prepaid_balance_usd:
                problems.append((("reserve_usd",), "must be < prepaid_balance_usd"))
        _raise_if_any("Budget", problems)
        return self


class Provider(_Model):
    kind: ProviderKind
    api_key: str
    base_url: str | None = None

    @field_validator("api_key")
    @classmethod
    def _key_is_reference(cls, value: str) -> str:
        # The rejected value must never reach an error message.
        if not is_key_reference(value):
            raise PydanticCustomError("inline_api_key", INLINE_KEY_MESSAGE)
        return value

    @field_validator("base_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value is not None and HTTPS_URL.fullmatch(value) is None:
            raise PydanticCustomError("https_url", "must be a URL starting with https://")
        return value

    @model_validator(mode="after")
    def _base_url_matches_kind(self) -> Self:
        if self.kind == "openai_compatible" and self.base_url is None:
            _raise_if_any("Provider", [(("base_url",), "required when kind = 'openai_compatible'")])
        if self.kind == "anthropic" and self.base_url is not None:
            _raise_if_any("Provider", [(("base_url",), "not allowed when kind = 'anthropic'")])
        return self


class ModelSpec(_Model):
    provider: ProviderName
    model: str = Field(min_length=1)
    price_in_per_m: float = Field(ge=0)
    price_out_per_m: float = Field(ge=0)
    efforts: list[Effort] = Field(min_length=1)

    # Set by Config from the model key ("tier2.mid" -> 2).
    _tier: int = PrivateAttr()

    @field_validator("efforts")
    @classmethod
    def _unique(cls, value: list[Effort]) -> list[Effort]:
        if len(set(value)) != len(value):
            raise PydanticCustomError("duplicate_effort", "efforts must not repeat")
        return value

    @property
    def tier(self) -> int:
        return self._tier


class CounterfactualRoute(_Model):
    adapter: AdapterName
    model: ModelKey
    effort: Effort


class Defaults(_Model):
    counterfactual_route: CounterfactualRoute


class RepoSettings(_Model):
    privacy_class: PrivacyClass = "private"
    provider_allow: list[ProviderName] | None = None


class Config(_Model):
    budget: Budget
    providers: dict[ProviderName, Provider] = Field(min_length=1)
    models: dict[ModelKey, ModelSpec] = Field(min_length=1)
    defaults: Defaults
    repo: RepoSettings = Field(default_factory=RepoSettings)

    @model_validator(mode="after")
    def _check_references(self) -> Self:
        problems: list[tuple[Loc, str]] = []
        for key, spec in self.models.items():
            if spec.provider not in self.providers:
                problems.append(
                    (("models", key, "provider"), f"unknown provider '{spec.provider}'")
                )

        route = self.defaults.counterfactual_route
        target = self.models.get(route.model)
        if target is None:
            problems.append(
                (("defaults", "counterfactual_route", "model"), f"unknown model '{route.model}'")
            )
        elif route.effort not in target.efforts:
            allowed = ", ".join(target.efforts)
            problems.append(
                (
                    ("defaults", "counterfactual_route", "effort"),
                    f"effort '{route.effort}' is not allowed for model '{route.model}'"
                    f" (allowed: {allowed})",
                )
            )

        for index, name in enumerate(self.repo.provider_allow or []):
            if name not in self.providers:
                problems.append((("repo", "provider_allow", index), f"unknown provider '{name}'"))

        _raise_if_any("Config", problems)
        for key, spec in self.models.items():
            spec._tier = int(key.split(".", 1)[0].removeprefix("tier"))
        return self


_FRIENDLY_MESSAGES = {
    "extra_forbidden": "unknown field",
    "missing": "required field is missing",
}


def issues_from_validation_error(error: ValidationError, file: str) -> list[ConfigIssue]:
    """Turn a Pydantic error into ConfigIssues without ever copying the rejected input."""
    issues = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        parts = [str(part) for part in detail["loc"]]
        message = _FRIENDLY_MESSAGES.get(detail["type"], detail["msg"])
        if parts and parts[-1] == "[key]":
            parts.pop()
            message = f"invalid name: {message}"
        issues.append(ConfigIssue(file=file, field=".".join(parts) or None, message=message))
    return issues


def parse_config(data: Mapping[str, Any], file: str = "merged") -> Config:
    """Validate raw TOML data into a Config, raising ConfigError with field-level issues."""
    try:
        return Config.model_validate(data)
    except ValidationError as error:
        # `from None` keeps the Pydantic error, which holds raw input, out of tracebacks.
        raise ConfigError(issues_from_validation_error(error, file)) from None
