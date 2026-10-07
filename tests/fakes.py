"""Fake provider backends for adapter tests. Nothing here touches the network.

``FakeProvider`` serves scripted responses through ``httpx.MockTransport`` and records
every request. ``FakeSleep`` records backoff delays instead of sleeping.
"""

import asyncio
import json
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from arpeggio_ai.adapters.base import (
    Adapter,
    AdapterContext,
    AttemptResult,
    AttemptSpec,
    Route,
    StepEvent,
)
from arpeggio_ai.config.loader import template_bytes
from arpeggio_ai.config.models import Config, parse_config

# Looks like a real key so a leak would be obvious, and is unique enough to grep for.
FAKE_KEY = "sk-arpeggio-fake-7f3c9a1e5b2d4c60"
KEY_ENV = {"DEEPSEEK_API_KEY": FAKE_KEY}
MONDAY_PEAK = datetime(2026, 10, 5, 2, 0, tzinfo=UTC)  # Monday 02:00 UTC, DeepSeek peak
MONDAY_OFFPEAK = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

Script = httpx.Response | type[Exception] | Callable[[httpx.Request], httpx.Response]


def deepseek_usage(prompt: int = 120, hit: int = 100, completion: int = 30) -> dict[str, Any]:
    """Usage in the shape documented at https://api-docs.deepseek.com/api/create-chat-completion."""
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": hit},
        "prompt_cache_hit_tokens": hit,
        "prompt_cache_miss_tokens": prompt - hit,
    }


def completion(
    content: str = "Hello!",
    model: str | None = "deepseek-flash",
    usage: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1_791_000_000,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        **extra,
    }
    if model is not None:
        body["model"] = model
    if usage is not None:
        body["usage"] = usage
    return body


def ok(content: str = "Hello!", **kwargs: Any) -> httpx.Response:
    kwargs.setdefault("usage", deepseek_usage())
    return httpx.Response(200, json=completion(content, **kwargs))


def status(code: int, headers: dict[str, str] | None = None, **body: Any) -> httpx.Response:
    payload = body or {"error": {"message": f"HTTP {code}", "type": "error"}}
    return httpx.Response(code, json=payload, headers=headers)


class FakeProvider:
    """Serves the scripted items in order. An exception class is raised for its turn."""

    def __init__(self, *script: Script) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("FakeProvider got more requests than scripted")
        item = self.script.pop(0)
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, type):
            raise item(f"fake {item.__name__}", request=request)  # type: ignore[call-arg]
        return item(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def template_config(name: str = "micro-deepseek", **budget: Any) -> Config:
    data = tomllib.loads(template_bytes(name).decode("utf-8"))
    data["budget"].update(budget)
    return parse_config(data)


def make_context(
    config: Config,
    provider: FakeProvider,
    *,
    env: dict[str, str] | None = None,
    at: datetime = MONDAY_PEAK,
    sleep: FakeSleep | None = None,
    random: float = 0.5,
) -> AdapterContext:
    return AdapterContext(
        config=config,
        clock=lambda: at,
        sleep=sleep or FakeSleep(),
        random=lambda: random,
        env=KEY_ENV if env is None else env,
        transport=provider.transport,
    )


def make_spec(
    model: str = "tier1.flash",
    effort: str = "low",
    *,
    task_id: str = "task",
    attempt_id: str = "attempt",
    prompt: str = "Say hello.",
    max_tokens: int = 64,
    **kwargs: Any,
) -> AttemptSpec:
    kwargs.setdefault("timeout_s", 60)
    kwargs.setdefault("max_steps", 10)
    return AttemptSpec(
        task_id=task_id,
        attempt_id=attempt_id,
        prompt=prompt,
        route=Route("api", model, effort),  # type: ignore[arg-type]
        max_tokens=max_tokens,
        **kwargs,
    )


async def _collect(adapter: Adapter, spec: AttemptSpec) -> tuple[list[StepEvent], AttemptResult]:
    events = [event async for event in adapter.run(spec)]
    return events, await adapter.result()


def collect(adapter: Adapter, spec: AttemptSpec) -> tuple[list[StepEvent], AttemptResult]:
    return asyncio.run(_collect(adapter, spec))
