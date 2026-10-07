"""One real call to a real provider. Opt-in only: it can cost real money.

The live test runs only with ``-m live``, ``ARPEGGIO_LIVE=1``, ``ARPEGGIO_LIVE_MODEL`` set to a
model key from your config (for example ``tier1.flash``) and that provider's key variable
set. ``ARPEGGIO_LIVE_MAX_TOKENS`` sets the output cap (default 16, at most 1024) and
``ARPEGGIO_LIVE_TIMEOUT_S`` the read timeout per request (default 60, at most 300). It reads
your real config from ``$ARPEGGIO_HOME`` (or ``~/.arpeggio``) and records the call in a
temporary database, never in your real one. See README.md, "Live smoke test".

The ``live_max_tokens`` tests below are ordinary tests and run by default.
"""

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from arpeggio_ai.adapters import registry
from arpeggio_ai.adapters.api import MAX_RETRIES
from arpeggio_ai.adapters.base import AdapterContext, AttemptSpec, Route
from arpeggio_ai.config.loader import load_config
from arpeggio_ai.core.errors import ConfigError
from arpeggio_ai.core.secrets import reference_name
from arpeggio_ai.orchestrator.attempts import run_attempt
from arpeggio_ai.paths import artifacts_dir, db_path
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import (
    create_attempt,
    create_task,
    ensure_repo,
    get_attempt,
    list_steps,
)

# Captured at import time: the autouse `home` fixture points ARPEGGIO_HOME at a temp dir.
REAL_HOME = os.environ.get("ARPEGGIO_HOME") or str(Path.home() / ".arpeggio")
MAX_TOKENS_DEFAULT = 16
MAX_TOKENS_CAP = 1024
TIMEOUT_S_DEFAULT = 60
TIMEOUT_S_CAP = 300


def _env_int(env: Mapping[str, str], name: str, default: int, cap: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if not 1 <= value <= cap:
        raise ValueError(f"{name} must be between 1 and {cap}, got {value}")
    return value


def live_max_tokens(env: Mapping[str, str]) -> int:
    """``ARPEGGIO_LIVE_MAX_TOKENS`` as an int: default 16, from 1 to 1024."""
    return _env_int(env, "ARPEGGIO_LIVE_MAX_TOKENS", MAX_TOKENS_DEFAULT, MAX_TOKENS_CAP)


def live_timeout_s(env: Mapping[str, str]) -> int:
    """``ARPEGGIO_LIVE_TIMEOUT_S`` (read timeout per request): default 60, from 1 to 300."""
    return _env_int(env, "ARPEGGIO_LIVE_TIMEOUT_S", TIMEOUT_S_DEFAULT, TIMEOUT_S_CAP)


def test_live_timeout_s() -> None:
    assert live_timeout_s({}) == 60
    assert live_timeout_s({"ARPEGGIO_LIVE_TIMEOUT_S": "180"}) == 180
    with pytest.raises(ValueError, match="between 1 and 300, got 301"):
        live_timeout_s({"ARPEGGIO_LIVE_TIMEOUT_S": "301"})


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, 16),
        ({"ARPEGGIO_LIVE_MAX_TOKENS": ""}, 16),
        ({"ARPEGGIO_LIVE_MAX_TOKENS": " 512 "}, 512),
    ],
)
def test_live_max_tokens(env: dict[str, str], expected: int) -> None:
    assert live_max_tokens(env) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("1025", "between 1 and 1024, got 1025"),
        ("0", "between 1 and 1024, got 0"),
        ("-5", "between 1 and 1024, got -5"),
        ("lots", "must be a whole number, got 'lots'"),
        ("1.5", "must be a whole number, got '1.5'"),
    ],
)
def test_live_max_tokens_rejects_bad_values(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        live_max_tokens({"ARPEGGIO_LIVE_MAX_TOKENS": raw})


def _response_details(payload: dict[str, Any]) -> tuple[str | None, int | None]:
    """finish_reason and completion_tokens_details.reasoning_tokens from the raw response."""
    response = payload.get("response")
    if not isinstance(response, dict):
        return None, None
    finish_reason = None
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
    usage = response.get("usage")
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    return finish_reason, reasoning


@pytest.mark.live
def test_one_tiny_call(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("ARPEGGIO_LIVE") != "1":
        pytest.skip("set ARPEGGIO_LIVE=1 to call a real provider")
    model_key = os.environ.get("ARPEGGIO_LIVE_MODEL")
    if not model_key:
        pytest.skip("set ARPEGGIO_LIVE_MODEL to a model key from your config")
    try:
        max_tokens = live_max_tokens(os.environ)
        timeout_s = live_timeout_s(os.environ)
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)

    monkeypatch.setenv("ARPEGGIO_HOME", REAL_HOME)
    try:
        config = load_config()
    except ConfigError as error:
        pytest.fail(
            f"{error}\nCreate it with `uv run arpeggio init --profile <name>`, replace the"
            " placeholder model ids, then run `uv run arpeggio config validate`.",
            pytrace=False,
        )
    monkeypatch.setenv("ARPEGGIO_HOME", str(home))
    model = config.models.get(model_key)
    assert model is not None, f"{model_key} is not in {REAL_HOME}/config.toml"
    api_key = config.providers[model.provider].api_key
    if api_key is not None and not os.environ.get(reference_name(api_key)):
        pytest.skip(f"{reference_name(api_key)} is not set")

    home.mkdir(parents=True)
    artifacts = ArtifactStore(artifacts_dir(home))
    conn = open_db(db_path(home))
    try:
        repo = ensure_repo(conn, str(home), "live-smoke")
        task = create_task(
            conn, repo.id, "Live smoke", "one tiny call", profile=config.budget.profile
        )
        effort = model.efforts[0]
        attempt = create_attempt(
            conn,
            task.id,
            adapter="api",
            model=model_key,
            effort=effort,
            verification="light",
            route_reason={"live_smoke": True},
            provider_model=model.model,
        )
        spec = AttemptSpec(
            task_id=task.id,
            attempt_id=attempt.id,
            prompt="Reply with the single word: ok",
            route=Route("api", model_key, effort),
            timeout_s=timeout_s,
            max_steps=1 + MAX_RETRIES,  # one call, plus the adapter's retries if it needs them
            max_tokens=max_tokens,
        )
        adapter = registry.create("api", AdapterContext(config=config))
        result = asyncio.run(run_attempt(conn, artifacts, adapter, spec))
        steps = [step for step in list_steps(conn, attempt.id) if step.kind == "model_call"]
        finished = get_attempt(conn, attempt.id)
    finally:
        conn.close()

    history = "; ".join(f"step {step.seq}: {step.summary}" for step in steps)
    assert result.status == "completed", f"{result.final_message} ({history or 'no steps'})"
    assert steps and finished is not None
    step = steps[-1]  # earlier steps, if any, were retried calls
    assert step.cost_usd is not None and step.cost_usd >= 0
    assert step.actual_model
    assert step.payload_ref is not None
    finish_reason, reasoning = _response_details(json.loads(artifacts.read(step.payload_ref)))
    print(
        f"\nrequested model   {model_key} ({model.model}), effort {effort},"
        f" max_tokens {max_tokens}, timeout {timeout_s}s"
        f"\nmodel calls       {len(steps)} (attempt cost ${finished.cost_usd:.8f})"
        f"\nactual_model      {step.actual_model}"
        f"\nmodel_mismatch    {finished.model_mismatch}"
        f"\nfinish_reason     {finish_reason}"
        f"\nprompt tokens     {step.input_tokens}"
        f"\ncache-hit tokens  {step.cached_tokens}"
        f"\ncompletion tokens {step.output_tokens}"
        f"\nreasoning tokens  {'not reported' if reasoning is None else reasoning}"
        f"\nprice window      {step.price_window} (multiplier {step.price_multiplier})"
        f"\nrecorded cost     ${step.cost_usd:.8f} (estimated: {step.cost_estimated})"
    )


def test_response_details() -> None:
    payload = {
        "response": {
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {
                "completion_tokens": 16,
                "completion_tokens_details": {"reasoning_tokens": 16},
            },
        }
    }
    assert _response_details(payload) == ("length", 16)
    assert _response_details({"response": {"choices": [], "usage": {}}}) == (None, None)
    assert _response_details({"response": "<html>"}) == (None, None)
