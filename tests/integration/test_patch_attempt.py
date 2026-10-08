"""run_patch_attempt end to end: temp git repo, fake provider, real worktree, checks and DB."""

import asyncio
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FAKE_KEY, FakeProvider, deepseek_usage, make_context, ok, status, template_config
from gitrepo import FILES, FIX, WRONG_FIX, make_repo, run_git, snapshot

from arpeggio_ai.adapters.base import Route
from arpeggio_ai.config.models import Config
from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.evals.task import EvalTask
from arpeggio_ai.orchestrator.attempts import (
    PatchAttemptOutcome,
    overhead_history,
    run_patch_attempt,
)
from arpeggio_ai.paths import artifacts_dir, db_path, logs_dir
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import (
    Attempt,
    Step,
    add_criterion,
    create_task,
    get_attempt,
    get_task,
    list_steps,
    list_verdicts,
    register_repo,
)

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]


class E2E:
    def __init__(self, home: Path, repo: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self.home, self.repo = home, repo
        self.conn = open_db(db_path(home))
        self.artifacts = ArtifactStore(artifacts_dir(home))
        configure_logging(logs_dir(home))
        registered = register_repo(self.conn, repo)
        self.task = create_task(
            self.conn, registered.id, "Fix add", "Make calc.add add.", profile="micro"
        )

    def criteria(self, *specs: dict[str, Any]) -> None:
        for spec in specs:
            add_criterion(self.conn, self.task.id, spec)

    def run(
        self,
        provider: FakeProvider,
        *,
        config: Config | None = None,
        model: str = "tier1.flash",
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> PatchAttemptOutcome:
        context = make_context(
            config or template_config(),
            provider,
            env=env,
            prompt_overhead=overhead_history(self.conn),
        )
        kwargs.setdefault("context_files", ["src/calc/ops.py"])
        return asyncio.run(
            run_patch_attempt(
                self.conn,
                self.artifacts,
                context,
                task_id=self.task.id,
                route=Route("api", model, "low"),
                home=self.home,
                max_tokens=256,
                **kwargs,
            )
        )

    def attempt(self, outcome: PatchAttemptOutcome) -> Attempt:
        assert outcome.attempt_id is not None
        attempt = get_attempt(self.conn, outcome.attempt_id)
        assert attempt is not None
        return attempt

    def steps(self, outcome: PatchAttemptOutcome) -> list[Step]:
        assert outcome.attempt_id is not None
        return list_steps(self.conn, outcome.attempt_id)

    def task_status(self) -> str:
        task = get_task(self.conn, self.task.id)
        assert task is not None
        return task.status

    def log_events(self) -> list[dict[str, Any]]:
        text = "".join(p.read_text("utf-8") for p in logs_dir(self.home).glob("*.jsonl"))
        return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def e2e(home: Path, tmp_path: Path) -> Iterator[E2E]:
    harness = E2E(home, make_repo(tmp_path / "repo"))
    harness.criteria(
        {"kind": "command", "argv": [*PYTEST, "tests/test_calc.py"]},
        {"kind": "command", "argv": [*PYTEST, "tests/test_ok.py"]},
    )
    yield harness
    harness.conn.close()


def assert_totals_match_steps(e2e: E2E, outcome: PatchAttemptOutcome) -> None:
    attempt, steps = e2e.attempt(outcome), e2e.steps(outcome)
    assert attempt.steps_count == len(steps)
    assert attempt.input_tokens == sum(s.input_tokens or 0 for s in steps)
    assert attempt.output_tokens == sum(s.output_tokens or 0 for s in steps)
    assert attempt.cost_usd == pytest.approx(sum(s.cost_usd or 0 for s in steps), abs=1e-12)


def test_correct_fix_passes_every_check(e2e: E2E) -> None:
    before = snapshot(e2e.repo)
    provider = FakeProvider(ok(f"Fixed.\n\n{FIX}\n", usage=deepseek_usage(400, 0, 60)))
    outcome = e2e.run(provider)

    assert (outcome.attempt_status, outcome.task_status, outcome.failure_reason) == (
        "completed",
        "awaiting_review",
        None,
    )
    assert e2e.task_status() == "awaiting_review"
    attempt = e2e.attempt(outcome)
    assert attempt.status == "completed" and attempt.failure_reason is None
    assert attempt.worktree == str(e2e.home / "worktrees" / attempt.id)
    assert attempt.branch == f"arpeggio/{e2e.task.id}/1"
    assert attempt.base_sha == before["head"]
    assert attempt.provider_model == "deepseek-flash"

    verdicts = list_verdicts(e2e.conn, attempt.id)
    assert [v.passed for v in verdicts] == [True, True] == [v.passed for v in outcome.verdicts]
    assert all(v.log_ref and v.kind == "check" for v in verdicts)

    prompt, step, patch = e2e.steps(outcome)
    assert (prompt.kind, prompt.summary) == ("message", "prompt: 1 context file, nothing redacted")
    assert step.kind == "model_call" and step.cost_usd and step.cost_usd > 0
    assert (patch.kind, patch.summary) == ("message", "patch: 1 file (line endings lf: 1)")
    assert patch.payload_ref == f"{e2e.task.id}/{attempt.id}/patch.diff"
    assert_totals_match_steps(e2e, outcome)

    worktree = Path(attempt.worktree)
    assert (
        run_git(worktree, "log", "-1", "--format=%an|%s")
        == f"Arpeggio|arpeggio: attempt {attempt.id}"
    )
    assert worktree.exists()  # kept for inspection
    assert snapshot(e2e.repo)["files"] == before["files"]
    assert snapshot(e2e.repo)["head"] == before["head"]


def test_prompt_carries_task_checks_context_and_system_message(e2e: E2E) -> None:
    provider = FakeProvider(ok(FIX))
    e2e.run(provider)
    body = provider.bodies()[0]
    system, user = body["messages"]
    assert system["role"] == "system" and "exactly one fenced code block" in system["content"]
    assert user["content"].startswith("Task:\nMake calc.add add.")
    assert "tests/test_calc.py` exits with code 0." in user["content"]
    assert "File `src/calc/ops.py`:\n````\ndef add(a, b):\n    return a - b" in user["content"]
    assert body["max_tokens"] == 256


def test_wrong_fix_fails_verification(e2e: E2E) -> None:
    outcome = e2e.run(FakeProvider(ok(WRONG_FIX)))
    assert (outcome.attempt_status, outcome.task_status) == ("completed", "failed")
    assert [v.passed for v in outcome.verdicts] == [False, True]
    detail = outcome.verdicts[0].detail
    assert detail is not None and detail["exit_code"] == 1
    assert e2e.task_status() == "failed"
    assert_totals_match_steps(e2e, outcome)


def test_model_claiming_success_without_a_diff_is_not_believed(e2e: E2E) -> None:
    outcome = e2e.run(FakeProvider(ok("All tests pass now, the bug is fixed.")))
    assert (outcome.attempt_status, outcome.task_status, outcome.failure_reason) == (
        "error",
        "failed",
        "patch_missing",
    )
    attempt = e2e.attempt(outcome)
    assert (attempt.status, attempt.failure_reason) == ("error", "patch_missing")
    _prompt, model_call, failure = e2e.steps(outcome)
    assert model_call.kind == "model_call"
    assert (
        failure.kind == "message"
        and failure.summary == "patch_missing: the reply has no ```diff block"
    )
    assert failure.payload_ref is not None
    assert list_verdicts(e2e.conn, attempt.id) == []
    assert_totals_match_steps(e2e, outcome)


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (FIX + "\n" + FIX, "patch_ambiguous"),
        ("```diff\n--- a/../x.py\n+++ b/../x.py\n@@ -1 +1 @@\n-a\n+b\n```", "patch_unsafe"),
        (
            "```diff\n--- a/src/calc/ops.py\n+++ b/src/calc/ops.py\n@@ -1,2 +1,2 @@\n"
            " def add(x, y):\n-    return x - y\n+    return x + y\n```",
            "patch_does_not_apply",
        ),
    ],
)
def test_patch_failures_store_a_reason(e2e: E2E, reply: str, reason: str) -> None:
    outcome = e2e.run(FakeProvider(ok(reply)))
    assert (outcome.attempt_status, outcome.task_status, outcome.failure_reason) == (
        "error",
        "failed",
        reason,
    )
    failure = e2e.steps(outcome)[-1]
    assert failure.summary is not None and failure.summary.startswith(f"{reason}: ")
    assert failure.payload_ref is not None
    assert e2e.artifacts.read(failure.payload_ref)


def test_guard_refusal_pauses_the_task(e2e: E2E) -> None:
    provider = FakeProvider()
    outcome = e2e.run(provider, config=template_config("free"), model="tier1.fast")
    assert (outcome.attempt_status, outcome.task_status) == ("paused", "paused")
    assert provider.requests == []
    attempt = e2e.attempt(outcome)
    assert run_git(Path(str(attempt.worktree)), "status", "--porcelain") == ""


def test_provider_error_fails_the_task(e2e: E2E) -> None:
    outcome = e2e.run(FakeProvider(status(401)))
    assert (outcome.attempt_status, outcome.task_status, outcome.failure_reason) == (
        "error",
        "failed",
        None,
    )


def test_no_criteria_needs_the_user(home: Path, tmp_path: Path) -> None:
    harness = E2E(home, make_repo(tmp_path / "repo"))
    try:
        provider = FakeProvider()
        outcome = harness.run(provider)
        assert outcome == PatchAttemptOutcome(None, None, "needs_user", "no_criteria", [])
        assert harness.task_status() == "needs_user"
        assert provider.requests == []
        assert harness.conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    finally:
        harness.conn.close()


def test_check_timeout_fails_the_task(home: Path, tmp_path: Path) -> None:
    harness = E2E(home, make_repo(tmp_path / "repo"))
    try:
        harness.criteria(
            {
                "kind": "command",
                "argv": [sys.executable, "-c", "import time; time.sleep(60)"],
                "timeout_s": 1,
            }
        )
        outcome = harness.run(FakeProvider(ok(FIX)))
        assert (outcome.attempt_status, outcome.task_status) == ("completed", "failed")
        [verdict] = outcome.verdicts
        assert verdict.detail is not None and verdict.detail["timed_out"] is True
    finally:
        harness.conn.close()


def test_worktree_failure_is_recorded(home: Path, tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    harness = E2E(home, plain)
    try:
        harness.criteria({"kind": "file_exists", "path": "README.md"})
        provider = FakeProvider()
        outcome = harness.run(provider)
        assert (outcome.attempt_status, outcome.task_status, outcome.failure_reason) == (
            "error",
            "failed",
            "worktree_failed",
        )
        assert provider.requests == []
    finally:
        harness.conn.close()


def test_checks_never_see_the_provider_key(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    harness = E2E(home, make_repo(tmp_path / "repo"))
    try:
        config = template_config()
        repo_settings = config.repo.model_copy(update={"check_env": ["DEEPSEEK_API_KEY"]})
        config = config.model_copy(update={"repo": repo_settings})
        script = "import os, sys; sys.exit(1 if 'DEEPSEEK_API_KEY' in os.environ else 0)"
        harness.criteria({"kind": "command", "argv": [sys.executable, "-c", script]})
        outcome = harness.run(FakeProvider(ok(FIX)), config=config)
        assert [v.passed for v in outcome.verdicts] == [True]
    finally:
        harness.conn.close()


def test_two_attempts_use_separate_worktrees(e2e: E2E) -> None:
    first = e2e.run(FakeProvider(ok(WRONG_FIX)))
    second = e2e.run(FakeProvider(ok(FIX)))
    a, b = e2e.attempt(first), e2e.attempt(second)
    assert a.worktree != b.worktree and a.branch != b.branch
    assert b.branch == f"arpeggio/{e2e.task.id}/2"
    assert (Path(str(a.worktree)) / "src/calc/ops.py").read_text().endswith("a * b\n")
    assert (Path(str(b.worktree)) / "src/calc/ops.py").read_text().endswith("a + b\n")
    assert second.task_status == "awaiting_review"


# Secrets (SAF-02, NFR-06)

# A key made only for these tests: none of its 8-character pieces occur in normal output.
LEAK_KEY = "tq9Vx2LmZ8rB4nW7cK1pYs5HdF3gJ6Qe"
SETTINGS = f'KEY = "{LEAK_KEY}"\nDEBUG = False\n'


def repo_with_settings(path: Path) -> Path:
    return make_repo(path, {**FILES, "src/calc/settings.py": SETTINGS})


def pieces(value: str, size: int = 8) -> set[str]:
    """Every substring of ``value`` with ``size`` characters (longer ones contain one)."""
    return {value[i : i + size] for i in range(len(value) - size + 1)}


def test_configured_key_never_leaks_from_an_end_to_end_patch_attempt(
    home: Path, tmp_path: Path
) -> None:
    harness = E2E(home, repo_with_settings(tmp_path / "repo"))
    try:
        # The first check prints the key so the 2,000-character verdict tail starts inside it.
        tail = "import sys; sys.stdout.write(open('src/calc/settings.py').read() + 'y' * 1965)"
        harness.criteria(
            {"kind": "command", "argv": [sys.executable, "-c", tail]},
            {"kind": "command", "argv": [*PYTEST, "tests/test_calc.py"]},
        )
        # A provider that echoes the key back in its reply.
        provider = FakeProvider(ok(f"Fixed, saw {LEAK_KEY} in settings.\n\n{FIX}\n"))
        outcome = harness.run(
            provider,
            env={"DEEPSEEK_API_KEY": LEAK_KEY},
            context_files=["src/calc/ops.py", "src/calc/settings.py"],
        )
        assert outcome.task_status == "awaiting_review"
        # Positive control: the key was really in play.
        assert provider.requests[0].headers["Authorization"] == f"Bearer {LEAK_KEY}"
        assert LEAK_KEY in (tmp_path / "repo" / "src/calc/settings.py").read_text()

        [user] = [m for m in provider.bodies()[0]["messages"] if m["role"] == "user"]
        assert 'KEY = "[REDACTED:configured_key]"' in user["content"]
        steps = harness.steps(outcome)
        assert steps[0].summary == ("prompt: 2 context files, 1 item redacted (configured_key: 1)")
        verdict = list_verdicts(harness.conn, outcome.attempt_id or "")[0]
        assert "configured_key]" in verdict.detail["output_tail"][:40]

        forbidden = pieces(LEAK_KEY)
        texts = [
            path.read_text("utf-8", errors="replace")
            for path in artifacts_dir(home).rglob("*")
            if path.is_file()
        ]
        texts += [path.read_text("utf-8") for path in logs_dir(home).glob("*.jsonl")]
        harness.conn.execute("PRAGMA wal_checkpoint(FULL)")
        tables = [
            row[0]
            for row in harness.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for table in tables:
            for row in harness.conn.execute(f'SELECT * FROM "{table}"'):
                texts.append(" ".join(str(value) for value in row))
        texts.append(db_path(home).read_bytes().decode("latin-1"))
        assert len(texts) > 10
        for text in texts:
            leaked = [piece for piece in forbidden if piece in text]
            assert leaked == [], text[:200]
        events = [e for e in harness.log_events() if e["event"] == "secret_scan.redacted"]
        sources = {e["source"] for e in events}
        assert {"context:src/calc/settings.py", "artifact:check-1.log"} <= sources
    finally:
        harness.conn.close()


def test_patch_on_redacted_lines_logs_and_does_not_apply(home: Path, tmp_path: Path) -> None:
    harness = E2E(home, repo_with_settings(tmp_path / "repo"))
    try:
        harness.criteria({"kind": "command", "argv": [*PYTEST, "tests/test_ok.py"]})
        diff = """```diff
--- a/src/calc/settings.py
+++ b/src/calc/settings.py
@@ -1,2 +1,2 @@
 KEY = "[REDACTED:configured_key]"
-DEBUG = False
+DEBUG = True
```"""
        outcome = harness.run(
            FakeProvider(ok(diff)),
            env={"DEEPSEEK_API_KEY": LEAK_KEY},
            context_files=["src/calc/settings.py"],
        )
        assert outcome.failure_reason == "patch_does_not_apply"
        [event] = [e for e in harness.log_events() if e["event"] == "patch.touches_redacted_lines"]
        assert event["paths"] == ["src/calc/settings.py"]
    finally:
        harness.conn.close()


def test_patch_copying_a_placeholder_fails_the_attempt(home: Path, tmp_path: Path) -> None:
    repo = repo_with_settings(tmp_path / "repo")
    harness = E2E(home, repo)
    try:
        harness.criteria({"kind": "command", "argv": [*PYTEST, "tests/test_ok.py"]})
        # The model saw the redacted file and wrote the placeholder into a new line.
        diff = """```diff
--- a/src/calc/settings.py
+++ b/src/calc/settings.py
@@ -2 +2,2 @@
 DEBUG = False
+BACKUP_KEY = "[REDACTED:configured_key]"
```"""
        outcome = harness.run(
            FakeProvider(ok(diff)),
            env={"DEEPSEEK_API_KEY": LEAK_KEY},
            context_files=["src/calc/settings.py"],
        )
        assert (outcome.attempt_status, outcome.task_status, outcome.failure_reason) == (
            "error",
            "failed",
            "patch_writes_redacted_placeholder",
        )
        failure = harness.steps(outcome)[-1]
        assert failure.summary == (
            "patch_writes_redacted_placeholder: added lines contain a [REDACTED:...]"
            " placeholder at src/calc/settings.py:3"
        )
        assert "BACKUP_KEY" not in failure.summary
        worktree = Path(str(harness.attempt(outcome).worktree))
        assert (worktree / "src/calc/settings.py").read_text() == SETTINGS
        assert run_git(worktree, "status", "--porcelain") == ""
    finally:
        harness.conn.close()


def test_hidden_tests_of_an_eval_task_never_reach_the_prompt(e2e: E2E) -> None:
    eval_task = EvalTask.model_validate(
        {
            "id": "calc-add",
            "category": "bugfix",
            "expected_risk": "low",
            "repo": {"path": str(e2e.repo), "base": "abcd123", "solution": "abcd124"},
            "request": "Make calc.add add.",
            "done_criteria": [{"kind": "command", "argv": ["python", "-V"]}],
            "hidden_tests": [{"path": "tests/test_calc.py", "source": "fixtures/x/test_calc.py"}],
        }
    )
    provider = FakeProvider(ok(FIX), ok(FIX))
    # Asked for as context (in another case, too): refused before it is read.
    e2e.run(provider, context_files=["src/calc/ops.py", "TESTS/test_calc.py"], eval_task=eval_task)
    e2e.run(provider, context_files=["tests/test_calc.py"], eval_task=eval_task)
    for body in provider.bodies():
        user = body["messages"][1]["content"]
        assert "assert add(2, 3) == 5" not in user
        assert "File `tests/test_calc.py`" not in user
        assert "File `TESTS/test_calc.py`" not in user
    assert "File `src/calc/ops.py`" in provider.bodies()[0]["messages"][1]["content"]
    refused = [e for e in e2e.log_events() if e.get("event") == "patch.context_refused_hidden_test"]
    assert len(refused) == 2
