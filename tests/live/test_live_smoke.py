"""One real call to a real provider. Opt-in only: it can cost real money.

Runs only with ``-m live``, ``ARPEGGIO_LIVE=1``, ``ARPEGGIO_LIVE_MODEL`` set to a model key
from your config (for example ``tier1.flash``) and that provider's key variable set. It
reads your real config from ``$ARPEGGIO_HOME`` (or ``~/.arpeggio``) and records the call in a
temporary database, never in your real one. See README.md, "Live smoke test".
"""

import asyncio
import os
from pathlib import Path

import pytest

from arpeggio_ai.adapters import registry
from arpeggio_ai.adapters.base import AdapterContext, AttemptSpec, Route
from arpeggio_ai.config.loader import load_config
from arpeggio_ai.core.secrets import reference_name
from arpeggio_ai.orchestrator.attempts import run_attempt
from arpeggio_ai.paths import artifacts_dir, db_path
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import create_attempt, create_task, ensure_repo, list_steps

# Captured at import time: the autouse `home` fixture points ARPEGGIO_HOME at a temp dir.
REAL_HOME = os.environ.get("ARPEGGIO_HOME") or str(Path.home() / ".arpeggio")
MAX_TOKENS = 16

pytestmark = pytest.mark.live


def test_one_tiny_call(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.environ.get("ARPEGGIO_LIVE") != "1":
        pytest.skip("set ARPEGGIO_LIVE=1 to call a real provider")
    model_key = os.environ.get("ARPEGGIO_LIVE_MODEL")
    if not model_key:
        pytest.skip("set ARPEGGIO_LIVE_MODEL to a model key from your config")

    monkeypatch.setenv("ARPEGGIO_HOME", REAL_HOME)
    config = load_config()
    monkeypatch.setenv("ARPEGGIO_HOME", str(home))
    model = config.models.get(model_key)
    assert model is not None, f"{model_key} is not in {REAL_HOME}/config.toml"
    api_key = config.providers[model.provider].api_key
    if api_key is not None and not os.environ.get(reference_name(api_key)):
        pytest.skip(f"{reference_name(api_key)} is not set")

    home.mkdir(parents=True)
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
            timeout_s=60,
            max_steps=1,
            max_tokens=MAX_TOKENS,
        )
        adapter = registry.create("api", AdapterContext(config=config))
        result = asyncio.run(run_attempt(conn, ArtifactStore(artifacts_dir(home)), adapter, spec))
        steps = [step for step in list_steps(conn, attempt.id) if step.kind == "model_call"]
    finally:
        conn.close()

    assert result.status == "completed", result.final_message
    assert len(steps) == 1
    step = steps[0]
    assert step.cost_usd is not None and step.cost_usd >= 0
    assert step.actual_model
    print(
        f"\n{model_key}: actual_model={step.actual_model} input={step.input_tokens}"
        f" cached={step.cached_tokens} output={step.output_tokens}"
        f" window={step.price_window} cost_usd={step.cost_usd:.8f}"
        f" estimated={step.cost_estimated}"
    )
