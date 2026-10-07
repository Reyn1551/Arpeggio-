"""The api adapter end to end: MockTransport provider, real SQLite store and artifacts."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import (
    FAKE_KEY,
    MONDAY_OFFPEAK,
    MONDAY_PEAK,
    FakeProvider,
    FakeSleep,
    collect,
    deepseek_usage,
    make_context,
    make_spec,
    ok,
    status,
    template_config,
)

from arpeggio_ai.adapters import registry
from arpeggio_ai.adapters.base import AttemptResult, AttemptSpec, StepEvent
from arpeggio_ai.config.models import Config, parse_config
from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.orchestrator.attempts import run_attempt
from arpeggio_ai.paths import artifacts_dir, db_path, logs_dir
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import (
    Attempt,
    Step,
    Task,
    create_attempt,
    create_task,
    ensure_repo,
    get_attempt,
    list_steps,
)


@dataclass
class Run:
    result: AttemptResult
    attempt: Attempt
    steps: list[Step]
    provider: FakeProvider
    sleep: FakeSleep


class Harness:
    def __init__(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self.home = home
        self.conn = open_db(db_path(home))
        self.artifacts = ArtifactStore(artifacts_dir(home))
        configure_logging(logs_dir(home))
        repo = ensure_repo(self.conn, "/code/app", "app")
        self.task: Task = create_task(self.conn, repo.id, "Greet", "say hello", profile="micro")

    def run(
        self,
        provider: FakeProvider,
        *,
        config: Config | None = None,
        model: str = "tier1.flash",
        effort: str = "low",
        **kwargs: Any,
    ) -> Run:
        context_args = {
            name: kwargs.pop(name) for name in ("env", "at", "random") if name in kwargs
        }
        sleep = FakeSleep()
        attempt = create_attempt(
            self.conn,
            self.task.id,
            adapter="api",
            model=model,
            effort=effort,  # type: ignore[arg-type]
            verification="light",
            route_reason={"test": True},
        )
        spec = make_spec(model, effort, task_id=self.task.id, attempt_id=attempt.id, **kwargs)
        context = make_context(config or template_config(), provider, sleep=sleep, **context_args)
        adapter = registry.create("api", context)
        result = asyncio.run(run_attempt(self.conn, self.artifacts, adapter, spec))
        current = get_attempt(self.conn, attempt.id)
        assert current is not None
        return Run(result, current, list_steps(self.conn, attempt.id), provider, sleep)

    def log_text(self) -> str:
        return "".join(path.read_text("utf-8") for path in logs_dir(self.home).glob("*.jsonl"))


@pytest.fixture
def harness(home: Path) -> Iterator[Harness]:
    h = Harness(home)
    yield h
    h.conn.close()


def assert_totals_match_steps(run: Run) -> None:
    attempt, steps = run.attempt, run.steps
    assert attempt.steps_count == len(steps)
    assert attempt.input_tokens == sum(step.input_tokens or 0 for step in steps)
    assert attempt.output_tokens == sum(step.output_tokens or 0 for step in steps)
    assert attempt.cached_tokens == sum(step.cached_tokens or 0 for step in steps)
    assert attempt.cost_usd == pytest.approx(sum(step.cost_usd or 0 for step in steps), abs=1e-12)


# 1. A DeepSeek-shaped success


def test_success_records_one_priced_step(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok("Hello there", usage=deepseek_usage(120, 100, 30))))
    assert (run.result.status, run.result.final_message) == ("completed", "Hello there")
    assert run.attempt.status == "completed" and run.attempt.finished_at is not None
    [step] = run.steps
    assert step.kind == "model_call" and step.summary == "Hello there"
    assert (step.input_tokens, step.cached_tokens, step.output_tokens) == (120, 100, 30)
    assert (step.price_window, step.price_multiplier) == ("peak", 1.0)
    assert (step.price_in_per_m, step.price_cache_hit_per_m, step.price_out_per_m) == (
        0.30,
        0.006,
        1.20,
    )
    # (20 * 0.30 + 100 * 0.006 + 30 * 1.20) / 1M = 42.6 / 1M
    assert step.cost_usd == 0.0000426
    assert (step.cost_estimated, step.actual_model) == (False, "deepseek-flash")
    assert run.attempt.model_mismatch is False
    assert_totals_match_steps(run)


def test_offpeak_call_is_billed_at_the_multiplier(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok(usage=deepseek_usage(120, 100, 30))), at=MONDAY_OFFPEAK)
    [step] = run.steps
    assert (step.price_window, step.price_multiplier, step.cost_usd) == ("offpeak", 0.5, 0.0000213)


def test_request_shape(harness: Harness) -> None:
    provider = FakeProvider(ok())
    harness.run(provider, model="tier3.pro", effort="max", system="Be brief.", max_tokens=64)
    [request] = provider.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert provider.bodies() == [
        {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Say hello."},
            ],
            "max_tokens": 64,
        }
    ]


def test_trailing_slash_in_base_url_is_not_doubled(harness: Harness) -> None:
    config = template_config()
    deepseek = config.providers["deepseek"].model_copy(
        update={"base_url": "https://api.deepseek.com/"}
    )
    config = config.model_copy(update={"providers": {"deepseek": deepseek}})
    provider = FakeProvider(ok())
    harness.run(provider, config=config)
    assert str(provider.requests[0].url) == "https://api.deepseek.com/chat/completions"


# 2. Multi-turn


def test_two_turn_conversation(harness: Harness) -> None:
    provider = FakeProvider(
        ok("first", usage=deepseek_usage(50, 0, 10)),
        ok("second", usage=deepseek_usage(80, 50, 20)),
    )
    run = harness.run(
        provider, model="tier2.flash", effort="medium", prompt="One?", follow_ups=["Two?"]
    )
    assert run.result.final_message == "second"
    assert [step.summary for step in run.steps] == ["first", "second"]
    assert provider.bodies()[1]["messages"] == [
        {"role": "user", "content": "One?"},
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": "Two?"},
    ]
    assert provider.bodies()[0]["reasoning_effort"] == "low"
    assert_totals_match_steps(run)
    assert run.attempt.cost_usd == pytest.approx(
        (50 * 0.30 + 10 * 1.20 + 30 * 0.30 + 50 * 0.006 + 20 * 1.20) / 1e6
    )


# 3. Retry-After


def test_429_with_retry_after_seconds(harness: Harness) -> None:
    provider = FakeProvider(status(429, {"Retry-After": "2"}), ok())
    run = harness.run(provider)
    assert run.result.status == "completed"
    assert run.sleep.calls == [2.0]
    assert len(provider.requests) == 2 and len(run.steps) == 1


def test_429_with_retry_after_http_date(harness: Harness) -> None:
    when = format_datetime(MONDAY_PEAK + timedelta(seconds=5), usegmt=True)
    run = harness.run(FakeProvider(status(429, {"Retry-After": when}), ok()))
    assert run.sleep.calls == [5.0]


def test_retry_after_above_the_cap_stops_retrying(harness: Harness) -> None:
    provider = FakeProvider(status(429, {"Retry-After": "121"}))
    run = harness.run(provider)
    assert (run.result.status, run.attempt.status) == ("error", "error")
    assert run.result.final_message == (
        "provider deepseek asked to wait 121s, more than max_quota_wait_s (120s)"
    )
    assert run.sleep.calls == [] and len(provider.requests) == 1


def test_unreadable_retry_after_falls_back_to_backoff(harness: Harness) -> None:
    run = harness.run(FakeProvider(status(503, {"Retry-After": "soon"}), ok()), random=0.25)
    assert run.sleep.calls == [0.25]


# 4. Retries exhausted


def test_four_500s_end_in_error_after_three_retries(harness: Harness) -> None:
    provider = FakeProvider(*(status(500) for _ in range(4)))
    run = harness.run(provider, random=0.5)
    assert (run.result.status, run.attempt.status) == ("error", "error")
    assert run.result.final_message == "provider deepseek returned HTTP 500 after 3 retries"
    assert len(provider.requests) == 4
    # Full jitter: random() * min(30, 1 * 2**n) with random() = 0.5.
    assert run.sleep.calls == [0.5, 1.0, 2.0]
    assert run.steps == []
    assert '"event": "adapter.retry"' in harness.log_text()


@pytest.mark.parametrize("code", [502, 503, 504])
def test_other_retryable_statuses(harness: Harness, code: int) -> None:
    run = harness.run(FakeProvider(status(code), ok()))
    assert run.result.status == "completed" and len(run.sleep.calls) == 1


# 5. Key errors


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_key_is_not_retried_and_names_the_variable(harness: Harness, code: int) -> None:
    provider = FakeProvider(status(code))
    run = harness.run(provider)
    assert run.result.status == "error"
    assert run.result.final_message == (
        f"provider deepseek rejected the API key (HTTP {code}); check env var DEEPSEEK_API_KEY"
    )
    assert len(provider.requests) == 1 and run.sleep.calls == []


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (400, "provider deepseek returned HTTP 400; not retried"),
        (404, "provider deepseek returned HTTP 404; not retried"),
        (422, "provider deepseek returned HTTP 422; not retried"),
        (
            402,
            "provider deepseek returned HTTP 402 (payment required, for example insufficient"
            " balance); not retried",
        ),
    ],
)
def test_client_errors_are_not_retried(harness: Harness, code: int, message: str) -> None:
    provider = FakeProvider(status(code))
    run = harness.run(provider)
    assert (run.result.status, run.result.final_message) == ("error", message)
    assert len(provider.requests) == 1


def test_unset_key_variable_sends_nothing(harness: Harness) -> None:
    provider = FakeProvider()
    run = harness.run(provider, env={})
    assert run.result.final_message == (
        "provider deepseek: environment variable DEEPSEEK_API_KEY is not set"
    )
    assert (run.attempt.status, provider.requests) == ("error", [])


def test_keychain_reference_is_not_supported_yet(harness: Harness) -> None:
    config = template_config()
    deepseek = config.providers["deepseek"].model_copy(update={"api_key": "keychain:deepseek"})
    config = config.model_copy(update={"providers": {"deepseek": deepseek}})
    provider = FakeProvider()
    run = harness.run(provider, config=config)
    assert run.result.final_message == (
        "provider deepseek: keychain references are not supported yet"
    )
    assert provider.requests == []


# 6. Missing usage


def test_missing_usage_is_estimated_and_flagged(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok("Hello!", usage=None)), prompt="Say hello.")
    [step] = run.steps
    # ceil(10 / 4) = 3 input tokens, ceil(6 / 4) = 2 output tokens.
    assert (step.input_tokens, step.cached_tokens, step.output_tokens) == (3, 0, 2)
    assert step.cost_estimated is True and run.attempt.cost_estimated is True
    assert step.cost_usd == round((3 * 0.30 + 2 * 1.20) / 1e6, 8)


def test_out_of_range_cache_hits_are_clamped_and_logged(harness: Harness) -> None:
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 50}
    run = harness.run(FakeProvider(ok(usage=usage)))
    [step] = run.steps
    assert (step.cached_tokens, step.cost_estimated) == (10, True)
    assert '"event": "adapter.usage_clamped"' in harness.log_text()


# 7. Actual model and mismatch (RTE-11, CFG-10)


def test_gateway_model_mismatch_is_flagged(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok(model="some-other-model")))
    assert run.result.status == "completed"
    assert run.steps[0].actual_model == "some-other-model"
    assert run.attempt.model_mismatch is True
    log = harness.log_text()
    assert (
        '"event": "adapter.model_mismatch"' in log and '"actual_model": "some-other-model"' in log
    )


@pytest.mark.parametrize("served", ["DeepSeek-V4.1-Flash", "deepseek-v4.1-flash", "DEEPSEEK-FLASH"])
def test_alias_or_requested_model_is_not_a_mismatch(harness: Harness, served: str) -> None:
    run = harness.run(FakeProvider(ok(model=served)))
    assert run.steps[0].actual_model == served
    assert run.attempt.model_mismatch is False


def test_absent_model_field_is_not_a_mismatch(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok(model=None)))
    assert run.steps[0].actual_model is None and run.attempt.model_mismatch is False


# 8. Loopback provider


def local_config() -> Config:
    data = template_config("free").model_dump(mode="json")
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
    return parse_config(data)


def test_loopback_provider_without_key_sends_no_authorization(harness: Harness) -> None:
    provider = FakeProvider(
        ok(model="qwen3:8b", usage={"prompt_tokens": 9, "completion_tokens": 4})
    )
    run = harness.run(provider, config=local_config(), model="tier1.local", env={})
    assert run.result.status == "completed"
    assert "authorization" not in provider.requests[0].headers
    assert str(provider.requests[0].url) == "http://localhost:11434/v1/chat/completions"
    [step] = run.steps
    assert (step.input_tokens, step.output_tokens, step.cost_usd) == (9, 4, 0.0)
    assert (step.price_window, step.price_in_per_m) == ("flat", 0.0)


def test_loopback_401_explains_there_is_no_key(harness: Harness) -> None:
    run = harness.run(FakeProvider(status(401)), config=local_config(), model="tier1.local")
    assert run.result.final_message == "provider ollama returned HTTP 401; it has no api_key set"


# 9. The key never leaks


def test_fake_key_never_reaches_artifacts_logs_or_db(harness: Harness) -> None:
    provider = FakeProvider(
        status(500, usage=deepseek_usage()),
        httpx.ReadTimeout,
        ok("first"),
        ok("second", model="other-model"),
        # A provider that echoes the key back must not get it into an artifact or message.
        status(401, error={"message": f"Invalid API key {FAKE_KEY}", "type": "auth"}),
    )
    run = harness.run(provider, follow_ups=["More.", "Again."])
    assert run.result.status == "error"
    # Positive control: the key really was sent, so the searches below mean something.
    assert all(r.headers["Authorization"] == f"Bearer {FAKE_KEY}" for r in provider.requests)
    assert len(run.steps) == 5
    assert run.result.final_message.endswith("Provider said: Invalid API key [REDACTED]")

    files = [p for p in artifacts_dir(harness.home).rglob("*") if p.is_file()]
    assert len(files) == 5
    for path in files + list(logs_dir(harness.home).glob("*.jsonl")):
        assert FAKE_KEY not in path.read_text("utf-8"), path
    assert FAKE_KEY not in run.result.final_message

    harness.conn.execute("PRAGMA wal_checkpoint(FULL)")
    tables = [
        row[0]
        for row in harness.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    for table in tables:
        for row in harness.conn.execute(f'SELECT * FROM "{table}"'):
            assert all(FAKE_KEY not in str(value) for value in row), table
    assert FAKE_KEY.encode() not in db_path(harness.home).read_bytes()


def test_artifact_holds_request_and_response_without_headers(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok("Hi")))
    [step] = run.steps
    assert step.payload_ref is not None
    payload = json.loads(harness.artifacts.read(step.payload_ref))
    assert set(payload) == {"request", "status", "response"}
    assert payload["request"]["messages"] == [{"role": "user", "content": "Say hello."}]
    assert payload["response"]["choices"][0]["message"]["content"] == "Hi"


# 10. Spend guard refusals send nothing


def refused(run: Run) -> None:
    assert run.provider.requests == []
    assert (run.result.status, run.attempt.status) == ("paused", "paused")
    [step] = run.steps
    assert (step.kind, step.cost_usd, step.input_tokens) == ("message", 0.0, None)
    assert run.result.final_message == step.summary  # short reasons fit the summary whole


def test_placeholder_model_is_refused(harness: Harness) -> None:
    run = harness.run(FakeProvider(), config=template_config("free"), model="tier1.fast")
    refused(run)
    assert run.steps[0].summary == (
        "spend guard: model tier1.fast still has a placeholder id; edit your config"
    )


def test_paid_model_under_free_profile_is_refused(harness: Harness) -> None:
    config = template_config()
    budget = config.budget.model_copy(
        update={"profile": "free", "per_task_usd": 0.0, "per_day_usd": 0.0, "per_month_usd": 0.0}
    )
    run = harness.run(FakeProvider(), config=config.model_copy(update={"budget": budget}))
    refused(run)
    assert (
        run.steps[0].summary
        == "spend guard: model tier1.flash is not free; current profile is free"
    )


def test_worst_case_over_budget_is_refused(harness: Harness) -> None:
    run = harness.run(FakeProvider(), model="tier3.pro", effort="high", max_tokens=1_000_000)
    refused(run)
    # "Say hello." is 10 chars, so the guard counts ceil(10 / 2) = 5 prompt tokens at $1.32/M,
    # plus 1M output tokens at $3.96/M.
    assert "worst-case cost $3.96000660" in run.steps[0].summary


def test_second_turn_refused_after_first_turn_spend(harness: Harness) -> None:
    config = template_config(per_task_usd=0.1)
    usage = deepseek_usage(10, 0, 20_000)  # 20k * 3.96 / 1M = $0.0792, plus 10 input tokens
    provider = FakeProvider(ok("one", usage=usage))
    run = harness.run(
        provider,
        config=config,
        model="tier3.pro",
        effort="high",
        max_tokens=10_000,  # worst case $0.0396 + input: fits $0.1 once, not after $0.0792
        follow_ups=["two"],
    )
    assert len(provider.requests) == 1
    assert run.result.status == "paused"
    assert [step.kind for step in run.steps] == ["model_call", "message"]
    assert_totals_match_steps(run)


# 11. Timeouts, transport errors, limits and bad routes


def test_read_timeout_is_recorded_as_estimated_then_retried(harness: Harness) -> None:
    run = harness.run(FakeProvider(httpx.ReadTimeout, ok()), prompt="x" * 400)
    assert run.result.status == "completed"
    first, second = run.steps
    assert first.summary == "read timeout; the provider may still bill this request"
    assert (first.input_tokens, first.output_tokens, first.cost_estimated) == (100, 0, True)
    assert first.cost_usd == round(100 * 0.30 / 1e6, 8)
    assert second.cost_estimated is False
    assert run.attempt.cost_estimated is True
    assert run.sleep.calls == [0.5]


def test_read_timeouts_exhaust_retries(harness: Harness) -> None:
    run = harness.run(FakeProvider(*([httpx.ReadTimeout] * 4)))
    assert run.result.final_message == (
        "provider deepseek timed out waiting for a response after 3 retries"
    )
    assert len(run.steps) == 4


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (httpx.ConnectTimeout, "timed out (ConnectTimeout)"),
        (httpx.ConnectError, "could not be reached (ConnectError)"),
    ],
)
def test_connection_failures_are_retried_without_a_step(
    harness: Harness, error: type[Exception], failure: str
) -> None:
    run = harness.run(FakeProvider(*([error] * 4)))
    assert run.result.final_message == f"provider deepseek {failure} after 3 retries"
    assert run.steps == []


def test_error_response_with_usage_is_billed(harness: Harness) -> None:
    run = harness.run(FakeProvider(status(503, usage=deepseek_usage(100, 0, 0)), ok()))
    first, _ = run.steps
    assert first.summary == "HTTP 503 from deepseek"
    assert first.cost_usd == round(100 * 0.30 / 1e6, 8)
    assert_totals_match_steps(run)


def test_unreadable_success_is_billed_then_fails(harness: Harness) -> None:
    body = {"model": "deepseek-flash", "usage": deepseek_usage(), "choices": []}
    run = harness.run(FakeProvider(httpx.Response(200, json=body)))
    assert run.result.final_message == "provider deepseek returned a response Arpeggio cannot read"
    assert len(run.steps) == 1 and run.steps[0].cost_usd == 0.0000426


def test_non_json_success_is_estimated_then_fails(harness: Harness) -> None:
    run = harness.run(FakeProvider(httpx.Response(200, text="<html>oops</html>")))
    assert run.result.status == "error"
    assert run.steps[0].cost_estimated is True


def test_null_content_is_an_empty_reply(harness: Harness) -> None:
    body = ok().json()
    body["choices"][0]["message"]["content"] = None
    run = harness.run(FakeProvider(httpx.Response(200, json=body)))
    assert (run.result.status, run.result.final_message) == ("completed", "")
    assert run.steps[0].summary == "(empty reply)"


def test_long_reply_summary_is_cut_to_200_chars(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok("y" * 500)))
    assert len(run.steps[0].summary or "") == 200 and run.steps[0].summary.endswith("...")  # type: ignore[union-attr]


def test_step_limit_ends_the_attempt_with_timeout(harness: Harness) -> None:
    provider = FakeProvider(ok("one"), ok("two"))
    run = harness.run(provider, follow_ups=["two", "three"], max_steps=2)
    assert (run.result.status, run.result.final_message) == (
        "timeout",
        "step limit reached (2 calls)",
    )
    assert len(provider.requests) == 2


def test_anthropic_provider_is_not_supported(harness: Harness, config_data: dict[str, Any]) -> None:
    config = parse_config(config_data)  # standard template: tier3.frontier is on anthropic
    provider = FakeProvider()
    run = harness.run(provider, config=config, model="tier3.frontier", effort="high")
    assert run.result.final_message == (
        "provider anthropic has kind 'anthropic'; the api adapter only supports"
        " openai_compatible providers"
    )
    assert provider.requests == []


def test_unknown_model_and_disallowed_effort(harness: Harness) -> None:
    assert harness.run(FakeProvider(), model="tier1.nope").result.final_message == (
        "unknown model 'tier1.nope'"
    )
    assert harness.run(FakeProvider(), effort="max").result.final_message == (
        "effort 'max' is not allowed for model tier1.flash"
    )


def test_effort_params_override_is_refused_by_the_adapter_too(harness: Harness) -> None:
    config = template_config()
    flash = config.models["tier1.flash"].model_copy(
        update={"effort_params": {"low": {"max_tokens": 1, "stream": True}}}
    )
    config = config.model_copy(update={"models": {**config.models, "tier1.flash": flash}})
    provider = FakeProvider()
    run = harness.run(provider, config=config)
    assert run.result.final_message == "effort_params may not set max_tokens, stream"
    assert provider.requests == []


# The orchestrator


class CrashingAdapter:
    name = "crash"

    def capabilities(self) -> dict[str, bool]:
        return {}

    async def run(self, spec: AttemptSpec) -> AsyncIterator[StepEvent]:
        yield StepEvent(kind="message", summary="about to fail", payload={})
        raise RuntimeError("boom")

    async def result(self) -> AttemptResult:
        raise AssertionError("not reached")

    async def cancel(self) -> None:
        return None

    def resume(self, attempt_id: str) -> AsyncIterator[StepEvent]:
        raise NotImplementedError


def test_crashing_adapter_marks_the_attempt_error_and_propagates(harness: Harness) -> None:
    attempt = create_attempt(
        harness.conn,
        harness.task.id,
        adapter="api",
        model="tier1.flash",
        effort="low",
        verification="light",
        route_reason={},
    )
    spec = make_spec(task_id=harness.task.id, attempt_id=attempt.id)
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run_attempt(harness.conn, harness.artifacts, CrashingAdapter(), spec))
    current = get_attempt(harness.conn, attempt.id)
    assert current is not None and current.status == "error"
    assert [step.summary for step in list_steps(harness.conn, attempt.id)] == ["about to fail"]
    assert '"event": "attempt.crashed"' in harness.log_text()


def test_log_lines_carry_task_and_attempt_ids(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok()))
    lines = [json.loads(line) for line in harness.log_text().splitlines()]
    request = next(line for line in lines if line["event"] == "adapter.request")
    assert (request["task_id"], request["attempt_id"]) == (harness.task.id, run.attempt.id)
    assert "Say hello." not in harness.log_text()  # no prompts in logs


def test_sqlite_rows_use_real_types(harness: Harness) -> None:
    run = harness.run(FakeProvider(ok()))
    row = harness.conn.execute(
        "SELECT typeof(cost_usd), typeof(price_multiplier), cost_estimated FROM steps WHERE id = ?",
        (run.steps[0].id,),
    ).fetchone()
    assert tuple(row) == ("real", "real", 0)
    assert isinstance(harness.conn, sqlite3.Connection)


def test_cancel_during_a_run_stops_before_the_next_request(harness: Harness) -> None:
    provider = FakeProvider()
    adapter = registry.create("api", make_context(template_config(), provider))

    async def reply_then_cancel(request: httpx.Request) -> httpx.Response:
        await adapter.cancel()
        return ok("one")

    provider.script = [reply_then_cancel]  # type: ignore[list-item]
    events, result = collect(adapter, make_spec(follow_ups=["two"]))
    assert (result.status, result.final_message) == ("cancelled", "cancelled by the user")
    assert len(events) == 1 and len(provider.requests) == 1


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("7", 7.0),
        (" 3 ", 3.0),
        ("Mon, 05 Oct 2026 02:00:09 GMT", 9.0),
        ("Mon, 05 Oct 2026 02:00:09 -0000", 9.0),  # no zone: read as UTC
        ("Mon, 05 Oct 2026 01:59:00 GMT", 0.0),  # already past
        ("tomorrow", None),
        (None, None),
    ],
)
def test_retry_after_parsing(header: str | None, seconds: float | None) -> None:
    from arpeggio_ai.adapters.api import retry_after_seconds

    assert retry_after_seconds(header, MONDAY_PEAK) == seconds


def test_default_context_uses_the_real_clock_and_sleep() -> None:
    from datetime import UTC, datetime

    from arpeggio_ai.adapters.base import AdapterContext

    context = AdapterContext(config=template_config())
    assert abs((context.clock() - datetime.now(UTC)).total_seconds()) < 5
    asyncio.run(context.sleep(0))
    assert context.transport is None


# Provider error details


MODEL_NOT_FOUND = {
    "error": {
        "message": "The model `llama-x` does not exist or you do not have access to it.",
        "type": "invalid_request_error",
        "code": "model_not_found",
    }
}


def test_fatal_error_keeps_the_provider_message_and_body(harness: Harness) -> None:
    run = harness.run(FakeProvider(status(404, **MODEL_NOT_FOUND)))
    assert run.result.final_message == (
        "provider deepseek returned HTTP 404; not retried. Provider said: The model `llama-x`"
        " does not exist or you do not have access to it."
    )
    [step] = run.steps
    assert (step.kind, step.summary, step.cost_usd) == ("message", "HTTP 404 from deepseek", 0.0)
    assert step.payload_ref is not None
    payload = json.loads(harness.artifacts.read(step.payload_ref))
    assert (payload["status"], payload["response"]) == (404, MODEL_NOT_FOUND)
    assert_totals_match_steps(run)


def test_rejected_key_message_also_carries_the_provider_message(harness: Harness) -> None:
    run = harness.run(FakeProvider(status(401, error={"message": "Invalid API Key"})))
    assert run.result.final_message == (
        "provider deepseek rejected the API key (HTTP 401); check env var DEEPSEEK_API_KEY."
        " Provider said: Invalid API Key"
    )


def test_non_json_error_body_is_kept_as_text(harness: Harness) -> None:
    run = harness.run(FakeProvider(httpx.Response(404, text="<html>Not Found</html>")))
    assert run.result.final_message == "provider deepseek returned HTTP 404; not retried"
    assert run.steps[0].payload_ref is not None
    payload = json.loads(harness.artifacts.read(run.steps[0].payload_ref))
    assert payload["response"] == "<html>Not Found</html>"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"error": {"message": "  bad\n  model  "}}, "bad model"),
        ({"error": "quota exceeded"}, "quota exceeded"),
        ({"message": "not found"}, "not found"),
        ({"error": {"message": ""}}, None),
        ({"error": {"code": 1}}, None),
        ({"detail": "x"}, None),
        ("text", None),
        (None, None),
        ({"error": {"message": "x" * 300}}, "x" * 197 + "..."),
        ({"error": {"message": f"key {FAKE_KEY} rejected"}}, "key [REDACTED] rejected"),
    ],
)
def test_error_detail(data: object, expected: str | None) -> None:
    from arpeggio_ai.adapters.api import _error_detail

    assert _error_detail(data, FAKE_KEY) == expected


def test_step_limit_message_names_the_last_failure(harness: Harness) -> None:
    run = harness.run(FakeProvider(httpx.ReadTimeout), max_steps=1)
    assert run.result.final_message == (
        "step limit reached (1 calls); the last request timed out waiting for a response"
    )
    assert run.steps[0].summary == "read timeout; the provider may still bill this request"
