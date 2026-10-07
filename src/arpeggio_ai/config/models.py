"""Pydantic models for ``config.toml``.

Single-field rules use Pydantic constraints and field validators. Rules that span fields
(budget rules per profile, base_url and api_key per provider, references between providers,
models and routes) run in ``model_validator(mode="after")`` hooks and report the exact field
at fault. The field reference lives in docs/05-ROUTING-AND-COST.md.
"""

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

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
DataUse = Literal["no_training", "may_train", "unknown"]

ProviderName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]*$")]
ModelKey = Annotated[str, StringConstraints(pattern=r"^tier[1-3]\.[a-z0-9_-]+$")]
EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]

API_KEY_REF = re.compile(r"(env|keychain):[A-Za-z_][A-Za-z0-9_.-]*")
INLINE_KEY_MESSAGE = "inline API keys are not allowed; use env:VAR or keychain:NAME"
DUPLICATE_PROVIDER_MESSAGE = (
    "duplicate provider endpoint; multiple accounts for the same provider are not supported"
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY = "|".join(_DAYS)
_PEAK_WINDOW = re.compile(
    rf"(?P<first>{_DAY})(?:-(?P<last>{_DAY}))? "
    r"(?P<h1>\d\d):(?P<m1>\d\d)-(?P<h2>\d\d):(?P<m2>\d\d)"
)

Loc = tuple[str | int, ...]


def is_key_reference(value: object) -> bool:
    """True if ``value`` is an ``env:VAR`` or ``keychain:NAME`` reference."""
    return isinstance(value, str) and API_KEY_REF.fullmatch(value) is not None


def is_loopback_url(url: str | None) -> bool:
    """True if ``url`` points at localhost, 127.0.0.1 or [::1]."""
    if url is None:
        return False
    try:
        return urlsplit(url).hostname in LOOPBACK_HOSTS
    except ValueError:
        return False


def normalize_endpoint(url: str | None) -> str | None:
    """Lowercase scheme and host, drop a trailing slash. Used to spot duplicate providers."""
    if url is None:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def peak_window_problem(window: str) -> str | None:
    """Why a ``peak_utc`` item such as ``"Mon-Fri 01:00-04:00"`` is invalid, or None."""
    match = _PEAK_WINDOW.fullmatch(window)
    if match is None:
        return "must look like 'Mon-Fri 01:00-04:00' (days Mon to Sun, 24-hour UTC times)"
    if match["last"] and _DAYS.index(match["last"]) <= _DAYS.index(match["first"]):
        return "day range must go forward within one week, for example Mon-Fri"
    h1, m1, h2, m2 = (int(match[group]) for group in ("h1", "m1", "h2", "m2"))
    if h1 > 23 or m1 > 59 or m2 > 59 or h2 > 24 or (h2 == 24 and m2 != 0):
        return "times must be between 00:00 and 23:59 (24:00 is allowed as an end time)"
    if h1 * 60 + m1 >= h2 * 60 + m2:
        return "start must be earlier than end (split windows that cross midnight)"
    return None


def peak_window_bounds(window: str) -> tuple[int, int, int, int]:
    """``"Mon-Fri 01:00-04:00"`` -> (first day, last day, start minute, end minute).

    Days count from Monday = 0 like ``datetime.weekday()``. The end minute is exclusive and
    may be 1440 (``24:00``). Raises ValueError on a window that would fail validation.
    """
    match = _PEAK_WINDOW.fullmatch(window)
    if match is None or peak_window_problem(window) is not None:
        raise ValueError(f"invalid peak window: {window!r}")
    first = _DAYS.index(match["first"])
    last = _DAYS.index(match["last"]) if match["last"] else first
    start = int(match["h1"]) * 60 + int(match["m1"])
    end = int(match["h2"]) * 60 + int(match["m2"])
    return first, last, start, end


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


class PricingWindows(_Model):
    """Peak windows in UTC. Model prices are peak prices; off-peak multiplies them."""

    peak_utc: list[str] | None = None
    offpeak_multiplier: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _check_windows(self) -> Self:
        problems: list[tuple[Loc, str]] = [
            (("peak_utc", index), problem)
            for index, window in enumerate(self.peak_utc or [])
            if (problem := peak_window_problem(window)) is not None
        ]
        if self.peak_utc and self.offpeak_multiplier is None:
            problems.append((("offpeak_multiplier",), "required when peak_utc is set"))
        _raise_if_any("PricingWindows", problems)
        return self


class Provider(_Model):
    kind: ProviderKind
    base_url: str | None = None
    api_key: str | None = None
    gateway: bool = False
    data_use: DataUse = "unknown"
    pricing_windows: PricingWindows | None = None
    # Input tokens the provider adds to every prompt (gateways may inject hidden context).
    prompt_overhead_tokens: int = Field(default=0, ge=0)

    @field_validator("api_key")
    @classmethod
    def _key_is_reference(cls, value: str | None) -> str | None:
        # The rejected value must never reach an error message.
        if value is not None and not is_key_reference(value):
            raise PydanticCustomError("inline_api_key", INLINE_KEY_MESSAGE)
        return value

    @field_validator("base_url")
    @classmethod
    def _https_or_loopback(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parts = urlsplit(value)
            scheme, host = parts.scheme, parts.hostname
        except ValueError:
            scheme, host = "", None
        allowed = scheme == "https" or (scheme == "http" and host in LOOPBACK_HOSTS)
        if host and allowed and not any(char.isspace() for char in value):
            return value
        raise PydanticCustomError(
            "https_url",
            "must be an https:// URL (http:// only for localhost, 127.0.0.1 or [::1])",
        )

    @model_validator(mode="after")
    def _check_endpoint(self) -> Self:
        problems: list[tuple[Loc, str]] = []
        if self.kind == "openai_compatible" and self.base_url is None:
            problems.append((("base_url",), "required when kind = 'openai_compatible'"))
        if self.api_key is None and not is_loopback_url(self.base_url):
            problems.append(
                (("api_key",), "required unless base_url is a loopback address (localhost)")
            )
        _raise_if_any("Provider", problems)
        return self


class Limits(_Model):
    """Free-tier caps: requests and tokens per minute and per day."""

    rpm: int | None = Field(default=None, gt=0)
    rpd: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)
    tpd: int | None = Field(default=None, gt=0)


_PRICES = ("price_in_per_m", "price_cache_hit_in_per_m", "price_out_per_m")
# Request fields the adapter owns. effort_params may not set them (CFG-08).
RESERVED_REQUEST_FIELDS = ("model", "messages", "max_tokens", "stream")


class ModelSpec(_Model):
    provider: ProviderName
    model: str = Field(min_length=1)
    efforts: list[Effort] = Field(min_length=1)
    # Opaque provider parameters per effort, passed to adapters as-is (CFG-08).
    effort_params: dict[Effort, dict[str, Any]] = Field(default_factory=dict)
    price_in_per_m: float = Field(ge=0)
    # Filled from price_in_per_m when absent, so it is only None if validation fails.
    price_cache_hit_in_per_m: float | None = Field(default=None, ge=0)
    price_out_per_m: float = Field(ge=0)
    free: bool = False
    limits: Limits = Field(default_factory=Limits)
    last_verified: date
    response_model_aliases: list[Annotated[str, StringConstraints(min_length=1)]] = Field(
        default_factory=list
    )
    # Overrides the provider's prompt_overhead_tokens for this model when set.
    prompt_overhead_tokens: int | None = Field(default=None, ge=0)

    # Set by Config from the model key ("tier2.mid" -> 2).
    _tier: int = PrivateAttr()

    @model_validator(mode="before")
    @classmethod
    def _default_cache_hit_price(cls, data: Any) -> Any:
        if isinstance(data, dict) and "price_cache_hit_in_per_m" not in data:
            price = data.get("price_in_per_m")
            if isinstance(price, int | float) and not isinstance(price, bool):
                return {**data, "price_cache_hit_in_per_m": price}
        return data

    @field_validator("efforts")
    @classmethod
    def _unique(cls, value: list[Effort]) -> list[Effort]:
        if len(set(value)) != len(value):
            raise PydanticCustomError("duplicate_effort", "efforts must not repeat")
        return value

    @field_validator("response_model_aliases")
    @classmethod
    def _unique_aliases(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise PydanticCustomError("duplicate_alias", "aliases must not repeat")
        return value

    @field_validator("last_verified", mode="before")
    @classmethod
    def _parse_date(cls, value: Any) -> Any:
        # TOML gives a date for `2026-10-07` and a str for "2026-10-07". Strict mode takes
        # only the former, so parse the string here. A datetime is a date subclass: reject it.
        if isinstance(value, datetime):
            raise PydanticCustomError("date_only", "must be a date like 2026-10-07, without a time")
        if isinstance(value, str):
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    return date.fromisoformat(value)
            except ValueError:
                pass
            raise PydanticCustomError("date_format", "must be a date like 2026-10-07")
        return value

    @field_validator("last_verified")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > date.today():
            raise PydanticCustomError("future_date", "must not be in the future")
        return value

    @model_validator(mode="after")
    def _check_prices_and_params(self) -> Self:
        problems: list[tuple[Loc, str]] = [
            (("effort_params", effort), "effort is not in this model's efforts")
            for effort in self.effort_params
            if effort not in self.efforts
        ]
        problems += [
            (("effort_params", effort, name), "set by Arpeggio; effort_params may not override it")
            for effort, params in self.effort_params.items()
            for name in RESERVED_REQUEST_FIELDS
            if name in params
        ]
        cache_hit = self.price_cache_hit_in_per_m
        if cache_hit is not None and cache_hit > self.price_in_per_m:
            problems.append((("price_cache_hit_in_per_m",), "must be <= price_in_per_m"))
        if self.free:
            problems += [
                ((name,), "must be 0 when free = true") for name in _PRICES if getattr(self, name)
            ]
        _raise_if_any("ModelSpec", problems)
        return self

    @property
    def tier(self) -> int:
        return self._tier


class CounterfactualRoute(_Model):
    adapter: AdapterName
    model: ModelKey
    effort: Effort


class Defaults(_Model):
    counterfactual_route: CounterfactualRoute


class PrivacySettings(_Model):
    """Global ``[privacy]`` table. Only allowed in the global config."""

    # Default opt-in for private repos to providers whose data_use is may_train or unknown.
    allow_training_providers: bool = False


class RepoSettings(_Model):
    privacy_class: PrivacyClass = "private"
    provider_allow: list[ProviderName] | None = None
    # None means "use [privacy] allow_training_providers" (client repos never do).
    allow_training_providers: bool | None = None
    # Extra environment variables checks may see (SAF-02). Provider key variables never pass.
    check_env: list[EnvVarName] = Field(default_factory=list)

    @field_validator("check_env")
    @classmethod
    def _unique_env(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise PydanticCustomError("duplicate_env", "check_env must not repeat a name")
        return value


class Config(_Model):
    budget: Budget
    providers: dict[ProviderName, Provider] = Field(min_length=1)
    models: dict[ModelKey, ModelSpec] = Field(min_length=1)
    defaults: Defaults
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    repo: RepoSettings = Field(default_factory=RepoSettings)

    def allows_training_providers(self) -> bool:
        """Effective opt-in to may_train/unknown providers for this repo (SAF-07).

        The repo value wins over the global [privacy] value. A client repo ignores the global
        value and needs an explicit repo-level true.
        """
        repo_value = self.repo.allow_training_providers
        if self.repo.privacy_class == "client":
            return repo_value is True
        if repo_value is not None:
            return repo_value
        return self.privacy.allow_training_providers

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

        if self.budget.profile == "free":
            problems += _free_profile_problems(self.models, self.providers)

        endpoints: set[tuple[str, str | None]] = set()
        for name, provider in self.providers.items():
            endpoint = (provider.kind, normalize_endpoint(provider.base_url))
            if endpoint in endpoints:
                problems.append((("providers", name), DUPLICATE_PROVIDER_MESSAGE))
            endpoints.add(endpoint)

        for index, name in enumerate(self.repo.provider_allow or []):
            if name not in self.providers:
                problems.append((("repo", "provider_allow", index), f"unknown provider '{name}'"))

        _raise_if_any("Config", problems)
        for key, spec in self.models.items():
            spec._tier = int(key.split(".", 1)[0].removeprefix("tier"))
        return self


def _free_profile_problems(
    models: Mapping[str, ModelSpec], providers: Mapping[str, Provider]
) -> list[tuple[Loc, str]]:
    """Under the free profile every model must cost nothing (BUD-02)."""
    problems: list[tuple[Loc, str]] = []
    for key, spec in models.items():
        provider = providers.get(spec.provider)
        local = provider is not None and is_loopback_url(provider.base_url)
        if not spec.free and not local:
            problems.append(
                (
                    ("models", key, "free"),
                    "must be true under profile 'free' (or use a local provider)",
                )
            )
        problems += [
            (("models", key, name), "must be 0 under profile 'free'")
            for name in _PRICES
            if getattr(spec, name)
        ]
    return problems


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
