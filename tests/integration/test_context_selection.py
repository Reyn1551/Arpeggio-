"""Automatic context selection end to end (M0.9): a real worktree, a real ``git ls-files``.

The milestone's claim is narrow and these tests hold it to exactly that: ``auto`` changes
which files are *shown* to the model, and nothing else. So the assertions are about the prompt
step's file list, and about the two rules that must not move — the hidden-test guard and
``patch_unknown_path``.
"""

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, make_context, ok, template_config
from gitrepo import FILES, FIX, make_repo, run_git

from arpeggio_ai.adapters.base import Route
from arpeggio_ai.core.logs import configure_logging
from arpeggio_ai.evals.task import EvalTask
from arpeggio_ai.orchestrator.attempts import overhead_history, run_patch_attempt
from arpeggio_ai.orchestrator.patch import (
    CONTEXT_MAX_FILES,
    read_context,
    select_context,
)
from arpeggio_ai.paths import artifacts_dir, db_path, logs_dir
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import (
    add_criterion,
    create_task,
    get_attempt,
    list_steps,
    register_repo,
)

PROMPT_PAYLOAD = "prompt-"
# ``git`` must be findable, so these calls get the ambient environment rather than ``{}``.
GIT_ENV = dict(os.environ)


def _context_payloads(home: Path, task_id: str) -> list[dict[str, Any]]:
    """Every recorded prompt payload for ``task_id``, oldest first."""
    records = sorted((artifacts_dir(home) / task_id).rglob(f"{PROMPT_PAYLOAD}*.json"))
    return [json.loads(path.read_text("utf-8")) for path in records]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "arpeggio-home"
    monkeypatch.setenv("ARPEGGIO_HOME", str(root))
    configure_logging(logs_dir(root))
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The shared fixture repo, plus an extra module the request will NOT mention."""
    files = dict(FILES)
    files["src/calc/format.py"] = "def fmt(value):\n    return str(value)\n"
    return make_repo(tmp_path / "repo", files)


def test_select_context_picks_the_named_file_not_the_loudest(repo: Path) -> None:
    """The request names calc/ops.py, so that is what comes back first."""
    chosen = asyncio.run(
        select_context(
            repo,
            "Fix the bug in src/calc/ops.py so calc.add adds.",
            env=GIT_ENV,
            home=repo,
        )
    )
    assert chosen[0] == "src/calc/ops.py"


def test_select_context_never_exceeds_the_caps(repo: Path) -> None:
    chosen = asyncio.run(
        select_context(
            repo,
            "Fix the bug in src/calc/ops.py tests/test_calc.py README.md",
            env=GIT_ENV,
            home=repo,
        )
    )
    assert len(chosen) <= CONTEXT_MAX_FILES
    assert set(chosen) <= set(FILES) | {"src/calc/format.py"}


def test_select_context_refuses_hidden_paths_before_ranking(repo: Path) -> None:
    """A hidden test is excluded from the candidates, not merely outranked."""
    chosen = asyncio.run(
        select_context(
            repo,
            "Fix the bug in src/calc/ops.py tests/test_calc.py",
            env=GIT_ENV,
            home=repo,
            hidden=["tests/test_calc.py"],
        )
    )
    assert "tests/test_calc.py" not in chosen
    assert "src/calc/ops.py" in chosen


def test_select_context_returns_nothing_when_the_request_names_nothing(repo: Path) -> None:
    assert asyncio.run(select_context(repo, "Fix it", env=GIT_ENV, home=repo)) == []


def test_select_context_is_deterministic_across_calls(repo: Path) -> None:
    request = "Fix the bug in src/calc/ops.py so calc.add adds."
    first = asyncio.run(select_context(repo, request, env=GIT_ENV, home=repo))
    second = asyncio.run(select_context(repo, request, env=GIT_ENV, home=repo))
    assert first == second


def test_select_context_on_a_non_repo_is_empty_not_an_error(tmp_path: Path) -> None:
    """A listing that fails is an empty selection, never an exception out of the attempt."""
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    assert asyncio.run(select_context(empty, "Fix src/calc/ops.py", env=GIT_ENV, home=empty)) == []


def test_selected_paths_are_readable_by_read_context(repo: Path) -> None:
    """Parity: whatever the selector proposes, ``read_context`` can actually read.

    The selector and the reader apply the same caps, so a proposal the reader rejects would
    mean the two disagree — the prompt would silently lose the file the ranking chose.
    """
    chosen = asyncio.run(
        select_context(repo, "Fix the bug in src/calc/ops.py", env=GIT_ENV, home=repo)
    )
    texts = read_context(repo, chosen)
    assert [path for path, _ in texts] == chosen


# Running a full attempt with context_selection="auto"


class Harness:
    def __init__(self, home: Path, repo: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self.home, self.repo = home, repo
        self.conn = open_db(db_path(home))
        self.artifacts = ArtifactStore(artifacts_dir(home))
        registered = register_repo(self.conn, repo)
        self.task = create_task(
            self.conn,
            registered.id,
            "Fix add",
            "Fix the bug in src/calc/ops.py so that calc.add adds instead of subtracting.",
            profile="micro",
        )
        add_criterion(
            self.conn,
            self.task.id,
            {"kind": "command", "argv": ["git", "--version"]},
        )

    def run(self, provider: FakeProvider, **kwargs: Any) -> Any:
        context = make_context(
            template_config(), provider, prompt_overhead=overhead_history(self.conn)
        )
        kwargs.setdefault("context_files", [])
        return asyncio.run(
            run_patch_attempt(
                self.conn,
                self.artifacts,
                context,
                task_id=self.task.id,
                route=Route("api", "tier1.flash", "low"),
                home=self.home,
                max_tokens=256,
                **kwargs,
            )
        )

    def prompt_files(self) -> list[str]:
        payloads = _context_payloads(self.home, self.task.id)
        assert payloads, "no prompt payload was recorded"
        return list(payloads[-1]["context_files"])


@pytest.fixture
def harness(home: Path, repo: Path) -> Iterator[Harness]:
    built = Harness(home, repo)
    yield built
    built.conn.close()


def test_auto_selection_fills_the_prompt_from_the_request(harness: Harness) -> None:
    """With no hand-written list, the request still puts the right file in the prompt."""
    provider = FakeProvider(ok(f"Fixed.\n\n{FIX}\n"))
    outcome = harness.run(provider, context_selection="auto")

    assert outcome.attempt_id is not None
    assert "src/calc/ops.py" in harness.prompt_files()
    # The model was asked for a diff against the file it was shown.
    body = json.loads(provider.requests[0].content)["messages"][-1]["content"]
    assert "src/calc/ops.py" in body


def test_manual_selection_is_unchanged_and_still_the_default(harness: Harness) -> None:
    """The default path keeps using exactly the caller's list, so nothing regresses."""
    provider = FakeProvider(ok(f"Fixed.\n\n{FIX}\n"))
    harness.run(provider, context_files=["src/calc/ops.py"], context_selection="manual")
    assert harness.prompt_files() == ["src/calc/ops.py"]


def test_manual_with_an_empty_list_shows_nothing(harness: Harness) -> None:
    """The old behaviour for a task with no context_files is preserved exactly."""
    provider = FakeProvider(ok(f"Fixed.\n\n{FIX}\n"))
    harness.run(provider, context_files=[], context_selection="manual")
    assert harness.prompt_files() == []


def test_auto_selection_still_refuses_a_hidden_test(harness: Harness, repo: Path) -> None:
    """A hidden test that the request names is excluded from the prompt even under auto."""
    task = EvalTask(
        id="hidden-guard",
        category="bugfix",
        expected_risk="low",
        repo={
            "path": str(repo),
            "base": run_git(repo, "rev-parse", "HEAD"),
            "solution": run_git(repo, "rev-parse", "HEAD"),
        },
        request="Fix the bug in src/calc/ops.py in the demo package.",
        done_criteria=[{"kind": "file_exists", "path": "src/calc/ops.py"}],
        hidden_tests=[{"path": "src/calc/ops.py", "source": "fixtures/ops.py"}],
    )
    provider = FakeProvider(ok(f"Fixed.\n\n{FIX}\n"))
    harness.run(provider, context_selection="auto", eval_task=task)
    assert "src/calc/ops.py" not in harness.prompt_files()


def test_auto_selection_does_not_forbid_an_unshown_file(tmp_path: Path, home: Path) -> None:
    """The milestone's central safety property.

    ``auto`` chooses what is shown; it must not turn "not shown" into "not allowed". M0.7's
    ``patch_unknown_path`` keys on the base commit, so a diff that touches a real tracked file
    the selector happened to leave out still applies rather than failing as unknown.

    ``tools/release/uploader.py`` is tracked but named nowhere in the request and shares no
    directory with it, so the selector leaves it out while it still exists at the base commit.
    """
    files = dict(FILES)
    files["tools/release/uploader.py"] = "def upload(path):\n    return path\n"
    built = Harness(home, make_repo(tmp_path / "repo", files))
    try:
        diff = """```diff
diff --git a/tools/release/uploader.py b/tools/release/uploader.py
--- a/tools/release/uploader.py
+++ b/tools/release/uploader.py
@@ -1,2 +1,2 @@
 def upload(path):
-    return path
+    return str(path)
```"""
        provider = FakeProvider(ok(f"Fixed.\n\n{diff}\n"))
        outcome = built.run(provider, context_selection="auto")
        shown = built.prompt_files()
        assert "tools/release/uploader.py" not in shown, "the selector must have left it out"
        # Not shown, yet still a real file at the base: it must not be "unknown".
        assert outcome.failure_reason != "patch_unknown_path"
    finally:
        built.conn.close()


def test_auto_selection_still_reports_a_genuinely_unknown_path(harness: Harness) -> None:
    """A path absent from the base commit is still ``patch_unknown_path`` under auto."""
    diff = """```diff
diff --git a/src/calc/does_not_exist.py b/src/calc/does_not_exist.py
--- a/src/calc/does_not_exist.py
+++ b/src/calc/does_not_exist.py
@@ -1,2 +1,2 @@
 def gone(a):
-    return a
+    return a + 1
```"""
    provider = FakeProvider(ok(f"Fixed.\n\n{diff}\n"))
    outcome = harness.run(provider, context_selection="auto")
    assert outcome.failure_reason in ("patch_unknown_path", "patch_does_not_apply")


def test_auto_selection_records_the_files_it_chose(harness: Harness) -> None:
    """The prompt step records the selection, so a report can explain what the model saw."""
    provider = FakeProvider(ok(f"Fixed.\n\n{FIX}\n"))
    outcome = harness.run(provider, context_selection="auto")
    steps = list_steps(harness.conn, outcome.attempt_id or "")
    prompt_steps = [step for step in steps if step.kind == "message"]
    assert prompt_steps, "no message step recorded"
    assert "context" in (prompt_steps[0].summary or "")
    attempt = get_attempt(harness.conn, outcome.attempt_id or "")
    assert attempt is not None
    assert attempt.worktree is not None
