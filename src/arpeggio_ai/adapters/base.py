"""The adapter contract every executor implements (EXE-01). See docs/03-ARCHITECTURE.md.

Core code depends on these types and on ``adapters.registry``, never on a concrete adapter.
Adapters do no disk or database I/O: they yield ``StepEvent``s, and the orchestrator
records them (``orchestrator.attempts.run_attempt``).
"""

import asyncio
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from arpeggio_ai.config.models import AdapterName, Config, Effort
from arpeggio_ai.cost.pricing import PriceSnapshot
from arpeggio_ai.store.repositories import AttemptStatus, StepKind, Verification

if TYPE_CHECKING:
    import httpx

# Keys every adapter's capabilities() returns, so the router can skip routes it cannot honor.
CAPABILITY_KEYS = frozenset({"effort_control", "token_reporting", "resume", "mcp", "streaming"})
SUMMARY_MAX_CHARS = 200


@dataclass(frozen=True, slots=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Route:
    adapter: AdapterName
    model: str  # logical model key from config, e.g. "tier2.flash"
    effort: Effort
    verification: Verification = "light"


@dataclass(frozen=True, slots=True)
class AttemptSpec:
    """What to run. ``prompt`` is the first user turn, ``follow_ups`` the later ones."""

    task_id: str
    attempt_id: str
    prompt: str
    route: Route
    timeout_s: int  # read timeout and wall-clock deadline of each request (EXE-06)
    max_steps: int
    max_tokens: int  # output cap per model call; required, no default
    worktree: str | None = None
    context_files: list[str] = field(default_factory=list)
    system: str | None = None
    follow_ups: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("timeout_s", "max_steps", "max_tokens"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


@dataclass(frozen=True, slots=True)
class StepEvent:
    """One recorded step. ``payload`` goes to an artifact file, never to the database."""

    kind: StepKind
    summary: str
    payload: dict[str, Any]
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None  # cache-hit input tokens
    cost_usd: float | None = None
    actual_model: str | None = None  # model named in the provider's response (RTE-11)
    cost_estimated: bool = False
    price: PriceSnapshot | None = None
    model_mismatch: bool = False
    provider: str | None = None  # provider that served a model call
    prompt_overhead_tokens: int | None = None  # input tokens beyond our prompt estimate


@dataclass(frozen=True, slots=True)
class AttemptResult:
    status: AttemptStatus
    final_message: str
    changed_files: list[str] = field(default_factory=list)


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _now() -> datetime:
    return datetime.now(UTC)


def _no_history(provider: str, model: str) -> int:
    return 0


@dataclass(frozen=True, slots=True)
class AdapterContext:
    """Everything an adapter needs from outside. Tests replace the clock, sleep and network."""

    config: Config
    clock: Callable[[], datetime] = _now
    sleep: Callable[[float], Awaitable[None]] = _sleep
    random: Callable[[], float] = random.random
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    transport: "httpx.AsyncBaseTransport | None" = None  # None means the real network
    # (provider, model key) -> highest recent prompt overhead; see orchestrator.attempts.
    prompt_overhead: Callable[[str, str], int] = _no_history


class Adapter(Protocol):
    name: str

    def capabilities(self) -> dict[str, bool]: ...

    def run(self, spec: AttemptSpec) -> AsyncIterator[StepEvent]: ...

    async def result(self) -> AttemptResult: ...

    async def cancel(self) -> None: ...

    def resume(self, attempt_id: str) -> AsyncIterator[StepEvent]: ...
