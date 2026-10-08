"""The public sample suite in evals/sample (EVL-01): it loads, its fixtures match the
generated repository, and every task passes `eval check` end to end."""

import asyncio
from pathlib import Path

import pytest
from samplerepo import SAMPLE_SUITE, git, make_sample_repo, sample_variables

from arpeggio_ai.evals.selfcheck import CheckRun
from arpeggio_ai.evals.suite import discover, summarize
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.store.artifacts import ArtifactStore


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    repo = tmp_path_factory.mktemp("sample") / "repo"
    return repo, make_sample_repo(repo)


def test_commits_are_deterministic(generated: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    _, commits = generated
    assert make_sample_repo(tmp_path / "again") == commits


def test_fixtures_match_the_generated_repository(generated: tuple[Path, dict[str, str]]) -> None:
    repo, commits = generated
    diff = git(repo, "diff", commits["c2"], commits["c3"]) + "\n"
    fixture = SAMPLE_SUITE / "fixtures/sample-docs-config/reference.diff"
    assert fixture.read_bytes() == diff.encode("utf-8")
    hidden = git(repo, "show", f"{commits['c2']}:tests/test_mul.py") + "\n"
    fixture = SAMPLE_SUITE / "fixtures/sample-feature-mul/hidden/tests/test_mul.py"
    assert fixture.read_bytes() == hidden.encode("utf-8")


def test_sample_tasks_load(generated: tuple[Path, dict[str, str]]) -> None:
    repo, commits = generated
    entries = discover(SAMPLE_SUITE, sample_variables(repo, commits))
    assert [(e.id, e.split, e.problems) for e in entries] == [
        ("sample-feature-mul", "tuning", []),
        ("sample-fix-add", "tuning", []),
        ("sample-docs-config", "holdout", []),
    ]
    categories = {e.id: e.task.category for e in entries if e.task is not None}
    assert categories == {
        "sample-feature-mul": "feature",
        "sample-fix-add": "bugfix",
        "sample-docs-config": "config",
    }
    assert summarize(entries).holdout == 1


def test_without_variables_the_sample_suite_is_invalid() -> None:
    assert all(entry.task is None for entry in discover(SAMPLE_SUITE))


def test_every_sample_task_passes_the_self_check(
    generated: tuple[Path, dict[str, str]], home: Path
) -> None:
    repo, commits = generated
    home.mkdir(parents=True, exist_ok=True)
    env = scrubbed_env()
    # No report is written, so the repository's sample folder stays clean.
    run = CheckRun(SAMPLE_SUITE, home, ArtifactStore(home / "artifacts"), lambda _t: env)
    entries = discover(SAMPLE_SUITE, sample_variables(repo, commits))

    async def check_all() -> list[str]:
        return [(await run.check(e, n)).status for n, e in enumerate(entries)]

    assert asyncio.run(check_all()) == ["valid", "valid", "valid"]
    assert list((home / "worktrees").iterdir()) == []
