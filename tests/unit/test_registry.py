from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from arpeggio_ai.adapters import registry
from arpeggio_ai.adapters.base import (
    Adapter,
    AdapterContext,
    AttemptResult,
    AttemptSpec,
    Route,
    StepEvent,
)
from arpeggio_ai.config.models import parse_config
from arpeggio_ai.core.errors import AdapterError


class FakeAdapter:
    name = "fake"

    def __init__(self, context: AdapterContext) -> None:
        self.context = context

    def capabilities(self) -> dict[str, bool]:
        return {}

    async def run(self, spec: AttemptSpec) -> AsyncIterator[StepEvent]:
        yield StepEvent(kind="message", summary="hi", payload={})

    async def result(self) -> AttemptResult:
        return AttemptResult(status="completed", final_message="")

    async def cancel(self) -> None:
        return None

    async def resume(self, attempt_id: str) -> AsyncIterator[StepEvent]:
        yield StepEvent(kind="message", summary="again", payload={})


@pytest.fixture
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(registry, "_factories", dict(registry._factories))
    yield


@pytest.fixture
def context(config_data: dict[str, Any]) -> AdapterContext:
    return AdapterContext(config=parse_config(config_data))


def test_register_and_create(clean_registry: None, context: AdapterContext) -> None:
    registry.register("fake", FakeAdapter)
    adapter: Adapter = registry.create("fake", context)
    assert isinstance(adapter, FakeAdapter) and adapter.context is context
    assert "fake" in registry.names()


def test_duplicate_registration_is_rejected(clean_registry: None) -> None:
    registry.register("fake", FakeAdapter)
    with pytest.raises(AdapterError, match="already registered"):
        registry.register("fake", FakeAdapter)


def test_unknown_adapter(context: AdapterContext) -> None:
    with pytest.raises(AdapterError, match="unknown adapter 'nope'"):
        registry.create("nope", context)


def test_unknown_adapter_with_empty_registry(
    monkeypatch: pytest.MonkeyPatch, context: AdapterContext
) -> None:
    monkeypatch.setattr(registry, "_factories", {})
    with pytest.raises(AdapterError, match=r"\(registered: none\)"):
        registry.create("api", context)


@pytest.mark.parametrize("name", ["timeout_s", "max_steps", "max_tokens"])
def test_attempt_spec_limits_must_be_positive(name: str) -> None:
    args: dict[str, Any] = {"timeout_s": 60, "max_steps": 5, "max_tokens": 100, name: 0}
    with pytest.raises(ValueError, match=f"{name} must be >= 1"):
        AttemptSpec(
            task_id="t", attempt_id="a", prompt="p", route=Route("api", "tier2.mid", "low"), **args
        )
