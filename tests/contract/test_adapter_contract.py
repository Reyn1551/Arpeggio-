"""Contract every registered adapter must meet (EXE-01, NFR-07), run against fake backends.

A new adapter must add a fake backend to ``BACKENDS``. Until it does, the coverage test
below fails.
"""

import asyncio
from collections.abc import Callable
from typing import get_args

import pytest
from fakes import FakeProvider, collect, make_context, make_spec, ok, template_config

from arpeggio_ai.adapters import registry
from arpeggio_ai.adapters.base import CAPABILITY_KEYS, Adapter, AttemptSpec
from arpeggio_ai.core.errors import AdapterError
from arpeggio_ai.store.repositories import AttemptStatus, StepKind

Backend = Callable[[], tuple[Adapter, AttemptSpec, FakeProvider]]


def api_backend() -> tuple[Adapter, AttemptSpec, FakeProvider]:
    provider = FakeProvider(ok("first"), ok("second"))
    adapter = registry.create("api", make_context(template_config(), provider))
    return adapter, make_spec(follow_ups=["Again."]), provider


BACKENDS: dict[str, Backend] = {"api": api_backend}


def test_every_registered_adapter_has_a_fake_backend() -> None:
    assert sorted(BACKENDS) == registry.names()


@pytest.fixture(params=registry.names())
def backend(request: pytest.FixtureRequest) -> tuple[str, Backend]:
    return request.param, BACKENDS[request.param]


def test_name_matches_registry(backend: tuple[str, Backend]) -> None:
    name, make = backend
    adapter, _, _ = make()
    assert adapter.name == name


def test_capabilities_report_every_key(backend: tuple[str, Backend]) -> None:
    adapter, _, _ = backend[1]()
    capabilities = adapter.capabilities()
    assert set(capabilities) == CAPABILITY_KEYS
    assert all(isinstance(value, bool) for value in capabilities.values())


def test_run_yields_valid_events_and_a_final_result(backend: tuple[str, Backend]) -> None:
    adapter, spec, _ = backend[1]()
    events, result = collect(adapter, spec)
    assert events
    assert all(event.kind in get_args(StepKind) for event in events)
    assert result.status in get_args(AttemptStatus) and result.status != "running"


def test_model_calls_carry_cost_fields(backend: tuple[str, Backend]) -> None:
    # CST-01: every model call has tokens, a non-negative cost and the prices applied.
    adapter, spec, _ = backend[1]()
    events, _ = collect(adapter, spec)
    calls = [event for event in events if event.kind == "model_call"]
    assert calls
    for event in calls:
        assert event.input_tokens is not None and event.input_tokens >= 0
        assert event.output_tokens is not None and event.output_tokens >= 0
        assert event.cached_tokens is not None and 0 <= event.cached_tokens <= event.input_tokens
        assert event.cost_usd is not None and event.cost_usd >= 0
        assert event.price is not None
        assert len(event.summary) <= 200


def test_result_before_run_is_an_error(backend: tuple[str, Backend]) -> None:
    adapter, _, _ = backend[1]()
    with pytest.raises(AdapterError):
        asyncio.run(adapter.result())


def test_cancel_before_run_sends_nothing(backend: tuple[str, Backend]) -> None:
    adapter, spec, provider = backend[1]()
    asyncio.run(adapter.cancel())
    events, result = collect(adapter, spec)
    assert (events, result.status, provider.requests) == ([], "cancelled", [])


def test_resume_matches_the_capability(backend: tuple[str, Backend]) -> None:
    adapter, _, _ = backend[1]()
    if adapter.capabilities()["resume"]:
        pytest.skip("adapter supports resume; covered by its own tests")
    with pytest.raises(AdapterError):
        adapter.resume("attempt")
