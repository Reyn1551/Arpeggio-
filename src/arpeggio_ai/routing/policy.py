"""Rule-based routing policy file (RTE-04, ADR-0003, ADR-0010).

The packaged ``policy.default.yaml`` is used unless ``<ARPEGGIO_HOME>/policy.yaml`` exists,
which replaces it entirely. Rules reference tiers and efforts, never model keys, so one
policy works under every profile. The first rule whose ``when`` matches the task wins.
A rule's ``route`` is the first attempt and ``escalation`` the routes tried, in order,
after a verification or patch failure.

Files are read with ``yaml.safe_load`` (ADR-0009) and validated with Pydantic
(unknown fields rejected). An invalid file is a ``ConfigError``.
"""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from arpeggio_ai.config.models import Effort, TaskCategory, issues_from_validation_error
from arpeggio_ai.core.errors import ConfigError, ConfigIssue

POLICY_FILENAME = "policy.yaml"
DEFAULT_SOURCE = "packaged:policy.default.yaml"
MAX_ESCALATIONS = 2  # three attempts in all

Risk = Literal["low", "medium", "high"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RouteRef(_Model):
    tier: Annotated[int, Field(ge=1, le=3)] | Literal["strongest"]
    effort: Effort


class When(_Model):
    risk: Risk | None = None
    category: list[TaskCategory] | None = None

    @field_validator("category", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    def matches(self, category: str, risk: str) -> bool:
        if self.risk is not None and self.risk != risk:
            return False
        return self.category is None or category in self.category


class Rule(_Model):
    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    when: When
    route: RouteRef
    escalation: list[RouteRef] = Field(default_factory=list, max_length=MAX_ESCALATIONS)


class PolicyFile(_Model):
    version: Literal[1]
    rules: list[Rule] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [rule.id for rule in self.rules]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError("duplicate_rule", "rule ids must be unique")
        return self

    def match(self, category: str, risk: str) -> Rule | None:
        return next((rule for rule in self.rules if rule.when.matches(category, risk)), None)


def parse_policy(text: str, source: str) -> PolicyFile:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError([ConfigIssue(source, None, f"not valid YAML: {error}")]) from None
    try:
        return PolicyFile.model_validate(data)
    except ValidationError as error:
        raise ConfigError(issues_from_validation_error(error, source)) from None


def load_policy(home: Path) -> tuple[PolicyFile, str]:
    """The active policy and where it came from (a path or ``DEFAULT_SOURCE``)."""
    override = home / POLICY_FILENAME
    if override.exists():
        try:
            text = override.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ConfigError([ConfigIssue(str(override), None, f"unreadable: {error}")]) from None
        return parse_policy(text, str(override)), str(override)
    text = files("arpeggio_ai.routing").joinpath("policy.default.yaml").read_text("utf-8")
    return parse_policy(text, DEFAULT_SOURCE), DEFAULT_SOURCE
