"""Direct API adapter for ``openai_compatible`` providers: non-streaming chat completions.

One ``model_call`` step per response that reports usage or succeeded, so retried calls are
billed too (CST-01). Before each request the spend guard runs (``cost.guard``). Failures
are retried with backoff and then end the attempt (RTE-12). The adapter never writes to
disk or the database, and never puts the API key, headers, prompts or response bodies in
logs or messages. Request and response bodies travel only in ``StepEvent.payload``, which
the orchestrator stores as an artifact.

See ADR-0007 and docs/05-ROUTING-AND-COST.md for the rules this implements.
"""

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from arpeggio_ai.adapters.base import (
    SUMMARY_MAX_CHARS,
    AdapterContext,
    AttemptResult,
    AttemptSpec,
    Message,
    StepEvent,
)
from arpeggio_ai.config.models import RESERVED_REQUEST_FIELDS, ModelSpec, Provider
from arpeggio_ai.core.errors import AdapterError, SecretNotFound, SpendRefused
from arpeggio_ai.core.logs import REDACTED
from arpeggio_ai.core.secrets import reference_name, resolve
from arpeggio_ai.cost.guard import check_call
from arpeggio_ai.cost.pricing import PriceSnapshot, compute_cost, normalize_usage, round_usd
from arpeggio_ai.store.repositories import AttemptStatus

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
CONNECT_TIMEOUT_S = 10.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
KEY_STATUSES = frozenset({401, 403})


class _Finished(Exception):
    """Ends the run with a result. Internal to this module."""

    def __init__(self, status: AttemptStatus, message: str) -> None:
        super().__init__(message)
        self.result = AttemptResult(status=status, final_message=message)


def _summary(text: str) -> str:
    return text if len(text) <= SUMMARY_MAX_CHARS else text[: SUMMARY_MAX_CHARS - 3] + "..."


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _scrub(value: Any, secret: str | None) -> Any:
    """``value`` with every occurrence of ``secret`` replaced, in case a provider echoes it."""
    if not secret:
        return value
    text = json.dumps(value)
    if secret not in text:
        return value
    return json.loads(text.replace(secret, REDACTED))


def _error_detail(data: Any, secret: str | None) -> str | None:
    """The provider's own error message (``error.message`` or ``message``), cut to one line."""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
    elif isinstance(error, str):
        message = error
    else:
        message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, str) or not message.strip():
        return None
    text = " ".join(message.split())
    if secret:
        text = text.replace(secret, REDACTED)
    return _summary(text)


def _content(data: Any) -> str | None:
    """The assistant text of a chat completion, or None if the body is not one."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if content is None:
        return ""
    return content if isinstance(content, str) else None


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (seconds or HTTP date), if readable."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max((moment - now).total_seconds(), 0.0)


def is_mismatch(actual: str | None, model: ModelSpec) -> bool:
    """True if the provider named a model other than the one requested or an alias (RTE-11)."""
    if actual is None:
        return False
    expected = {
        model.model.casefold(),
        *(alias.casefold() for alias in model.response_model_aliases),
    }
    return actual.casefold() not in expected


@dataclass(frozen=True, slots=True, kw_only=True)
class _Outcome:
    """What one request produced: a step to record, then a reply, a fatal error or a retry."""

    event: StepEvent | None = None
    reply: str | None = None
    status: int | None = None
    failure: str | None = None  # why to retry, for example "HTTP 503"
    fatal: str | None = None  # ends the attempt with status error
    retry_after: float | None = None


class ApiAdapter:
    name = "api"

    def __init__(self, context: AdapterContext) -> None:
        self._context = context
        self._result: AttemptResult | None = None
        self._cancelled = False
        self._spent_usd = 0.0
        self._secret: str | None = None  # only to scrub provider echoes; never logged

    def capabilities(self) -> dict[str, bool]:
        return {
            "effort_control": True,
            "token_reporting": True,
            "resume": False,
            "mcp": False,
            "streaming": False,
        }

    async def result(self) -> AttemptResult:
        if self._result is None:
            raise AdapterError("result() is only available after run() has finished")
        return self._result

    async def cancel(self) -> None:
        self._cancelled = True

    def resume(self, attempt_id: str) -> AsyncIterator[StepEvent]:
        raise AdapterError("the api adapter cannot resume attempts")

    async def run(self, spec: AttemptSpec) -> AsyncIterator[StepEvent]:
        self._result = None
        self._spent_usd = 0.0
        try:
            async for event in self._run(spec):
                if event.cost_usd:
                    self._spent_usd += event.cost_usd
                yield event
        except _Finished as finished:
            self._result = finished.result
        else:  # pragma: no cover - _run always ends with _Finished
            raise AdapterError("api adapter run ended without a result")

    async def _run(self, spec: AttemptSpec) -> AsyncIterator[StepEvent]:
        config = self._context.config
        key = spec.route.model
        model = config.models.get(key)
        if model is None:
            raise _Finished("error", f"unknown model {key!r}")
        provider = config.providers[model.provider]
        self._check_route(spec, model, provider)
        if self._cancelled:
            raise _Finished("cancelled", "cancelled before the first request")
        headers: dict[str, str] | None = None  # resolved after the first guard check

        url = f"{(provider.base_url or '').rstrip('/')}/chat/completions"
        timeout = httpx.Timeout(CONNECT_TIMEOUT_S, read=float(spec.timeout_s))
        messages = [Message("system", spec.system)] if spec.system else []
        reply = ""
        sent = 0
        async with httpx.AsyncClient(transport=self._context.transport, timeout=timeout) as client:
            for turn in [spec.prompt, *spec.follow_ups]:
                messages.append(Message("user", turn))
                body: dict[str, Any] = {
                    **model.effort_params.get(spec.route.effort, {}),
                    "model": model.model,
                    "messages": [asdict(message) for message in messages],
                    "max_tokens": spec.max_tokens,
                }
                retries = 0
                last_failure: str | None = None
                while True:
                    if self._cancelled:
                        raise _Finished("cancelled", "cancelled by the user")
                    if sent >= spec.max_steps:
                        limit = f"step limit reached ({spec.max_steps} calls)"
                        if last_failure is not None:
                            limit += f"; the last request {last_failure}"
                        raise _Finished("timeout", limit)
                    price, refusal = self._guard(key, messages, spec)
                    if refusal is not None:
                        yield refusal
                        reason = refusal.payload["spend_refused"]
                        raise _Finished("paused", f"spend guard: {reason}")
                    assert price is not None
                    if headers is None:
                        headers = self._headers(model.provider, provider)
                    sent += 1
                    log.info(
                        "adapter.request",
                        extra={"provider": model.provider, "model": key, "try": retries + 1},
                    )
                    outcome = await self._send(client, url, body, headers, price, model, spec)
                    if outcome.event is not None:
                        yield outcome.event
                    if outcome.fatal is not None:
                        raise _Finished("error", outcome.fatal)
                    if outcome.reply is not None:
                        reply = outcome.reply
                        messages.append(Message("assistant", reply))
                        break
                    delay = self._retry_delay(outcome, retries, model.provider)
                    log.warning(
                        "adapter.retry",
                        extra={
                            "provider": model.provider,
                            "status": outcome.status,
                            "failure": outcome.failure,
                            "retry": retries + 1,
                            "delay_s": round(delay, 3),
                        },
                    )
                    await self._context.sleep(delay)
                    retries += 1
                    last_failure = outcome.failure
        raise _Finished("completed", reply)

    def _check_route(self, spec: AttemptSpec, model: ModelSpec, provider: Provider) -> None:
        if provider.kind != "openai_compatible":
            raise _Finished(
                "error",
                f"provider {model.provider} has kind {provider.kind!r}; the api adapter only"
                " supports openai_compatible providers",
            )
        if spec.route.effort not in model.efforts:
            raise _Finished(
                "error", f"effort {spec.route.effort!r} is not allowed for model {spec.route.model}"
            )
        params = model.effort_params.get(spec.route.effort, {})
        clash = sorted(set(params) & set(RESERVED_REQUEST_FIELDS))
        if clash:
            raise _Finished("error", f"effort_params may not set {', '.join(clash)}")

    def _headers(self, provider_name: str, provider: Provider) -> dict[str, str]:
        if provider.api_key is None:
            return {}
        try:
            secret = resolve(provider.api_key, self._context.env)
        except (SecretNotFound, NotImplementedError, ValueError) as exc:
            raise _Finished("error", f"provider {provider_name}: {exc}") from None
        self._secret = secret.get_secret_value()
        return {"Authorization": f"Bearer {self._secret}"}

    def _guard(
        self, key: str, messages: list[Message], spec: AttemptSpec
    ) -> tuple[PriceSnapshot | None, StepEvent | None]:
        try:
            price = check_call(
                self._context.config,
                key,
                prompt_chars=sum(len(message.content) for message in messages),
                max_tokens=spec.max_tokens,
                spent_usd=self._spent_usd,
                at=self._context.clock(),
            )
        except SpendRefused as exc:
            log.warning("adapter.spend_refused", extra={"model": key, "reason": str(exc)})
            refusal = StepEvent(
                kind="message",
                summary=_summary(f"spend guard: {exc}"),
                payload={"spend_refused": str(exc), "model": key},
                cost_usd=0.0,
            )
            return None, refusal
        return price, None

    async def _send(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: dict[str, Any],
        headers: Mapping[str, str],
        price: PriceSnapshot,
        model: ModelSpec,
        spec: AttemptSpec,
    ) -> _Outcome:
        prompt_chars = sum(len(message["content"]) for message in body["messages"])
        try:
            response = await client.post(url, json=body, headers=dict(headers))
        except httpx.ReadTimeout:
            # The provider got the request and may bill it: record an estimated input cost.
            event = self._model_call(
                body, None, None, price, model, prompt_chars, 0, read_timeout=True
            )
            return _Outcome(event=event, failure="timed out waiting for a response")
        except httpx.TimeoutException as exc:
            return _Outcome(failure=f"timed out ({type(exc).__name__})")
        except httpx.TransportError as exc:
            return _Outcome(failure=f"could not be reached ({type(exc).__name__})")

        data = _json(response)
        usage = data.get("usage") if isinstance(data, dict) else None
        status = response.status_code
        if status == 200:
            content = _content(data)
            event = self._model_call(
                body, status, data, price, model, prompt_chars, len(content or "")
            )
            if content is None:
                fatal = f"provider {model.provider} returned a response Arpeggio cannot read"
                return _Outcome(event=event, status=status, fatal=fatal)
            return _Outcome(event=event, reply=content, status=status)

        # Errors are billed only when the provider reports usage for them.
        billed: StepEvent | None = None
        if isinstance(usage, dict):
            billed = self._model_call(body, status, data, price, model, prompt_chars, 0)
        if status not in RETRY_STATUSES:
            fatal = self._failure_message(model.provider, status)
            detail = _error_detail(data, self._secret)
            if detail:
                fatal = f"{fatal}. Provider said: {detail}"
            if billed is None:
                # Keep the provider's error body, so the failure can be diagnosed later.
                response_body = data if data is not None else response.text[:2000]
                billed = StepEvent(
                    kind="message",
                    summary=_summary(f"HTTP {status} from {model.provider}"),
                    payload={
                        "request": body,
                        "status": status,
                        "response": _scrub(response_body, self._secret),
                    },
                    cost_usd=0.0,
                )
            return _Outcome(event=billed, status=status, fatal=fatal)
        return _Outcome(
            event=billed,
            status=status,
            failure=f"HTTP {status}",
            retry_after=retry_after_seconds(
                response.headers.get("Retry-After"), self._context.clock()
            ),
        )

    def _model_call(
        self,
        body: dict[str, Any],
        status: int | None,
        data: Any,
        price: PriceSnapshot,
        model: ModelSpec,
        prompt_chars: int,
        output_chars: int,
        *,
        read_timeout: bool = False,
    ) -> StepEvent:
        raw_usage = data.get("usage") if isinstance(data, dict) else None
        usage = normalize_usage(raw_usage, prompt_chars, output_chars)
        if usage.clamped:
            log.warning("adapter.usage_clamped", extra={"provider": model.provider})
        output_tokens = 0 if read_timeout else usage.output_tokens
        cost = round_usd(
            compute_cost(price, usage.input_tokens, usage.cache_hit_tokens, output_tokens)
        )
        actual = data.get("model") if isinstance(data, dict) else None
        actual = actual if isinstance(actual, str) and actual else None
        if read_timeout:
            summary = "read timeout; the provider may still bill this request"
        elif status == 200:
            summary = _summary(_content(data) or "(empty reply)")
        else:
            summary = f"HTTP {status} from {model.provider}"
        return StepEvent(
            kind="model_call",
            summary=summary,
            payload={"request": body, "status": status, "response": _scrub(data, self._secret)},
            input_tokens=usage.input_tokens,
            output_tokens=output_tokens,
            cached_tokens=usage.cache_hit_tokens,
            cost_usd=cost,
            actual_model=actual,
            cost_estimated=usage.estimated or read_timeout,
            price=price,
            model_mismatch=is_mismatch(actual, model),
        )

    def _failure_message(self, provider_name: str, status: int) -> str:
        provider = self._context.config.providers[provider_name]
        if status in KEY_STATUSES:
            if provider.api_key is None:
                return f"provider {provider_name} returned HTTP {status}; it has no api_key set"
            name = reference_name(provider.api_key)
            return (
                f"provider {provider_name} rejected the API key (HTTP {status});"
                f" check env var {name}"
            )
        if status == 402:
            return (
                f"provider {provider_name} returned HTTP 402 (payment required, for example"
                " insufficient balance); not retried"
            )
        return f"provider {provider_name} returned HTTP {status}; not retried"

    def _retry_delay(self, outcome: _Outcome, retries: int, provider_name: str) -> float:
        what = outcome.failure or "failed"
        if retries >= MAX_RETRIES:
            if outcome.status is not None:
                what = f"returned HTTP {outcome.status}"
            raise _Finished("error", f"provider {provider_name} {what} after {retries} retries")
        cap = self._context.config.budget.max_quota_wait_s
        if outcome.retry_after is not None:
            if outcome.retry_after > cap:
                raise _Finished(
                    "error",
                    f"provider {provider_name} asked to wait {outcome.retry_after:.0f}s, more"
                    f" than max_quota_wait_s ({cap}s)",
                )
            return outcome.retry_after
        return self._context.random() * min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0**retries)
