"""CRLF-aware patch apply (EXE-08): per-file line endings, .gitattributes, stored diffs."""

import asyncio
from pathlib import Path

import pytest
from fakes import FakeProvider, make_context, ok, template_config
from gitrepo import FILES, FIX, make_repo

from arpeggio_ai.adapters.base import Route
from arpeggio_ai.orchestrator.attempts import overhead_history, run_patch_attempt
from arpeggio_ai.orchestrator.patch import (
    LineEndingPlan,
    PatchError,
    apply_patch,
    classify_line_endings,
    extract_patch,
    new_file_ending,
    normalize_line_endings,
    parse_diff,
    redacted_hunk_paths,
)
from arpeggio_ai.paths import artifacts_dir, db_path
from arpeggio_ai.safety.process import scrubbed_env
from arpeggio_ai.safety.worktree import Worktree, create_worktree
from arpeggio_ai.store.artifacts import ArtifactStore
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import add_criterion, create_task, register_repo

ENV = scrubbed_env()
CRLF_OPS = "def add(a, b):\r\n    return a - b\r\n"
OPS = "src/calc/ops.py"


def worktree(tmp_path: Path, home: Path, files: dict[str, str]) -> Worktree:
    home.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path / "repo", {**FILES, **files})
    return asyncio.run(
        create_worktree(repo, home, task_id="01T", attempt_id="01A", attempt_seq=1, env=ENV)
    )


def apply(diff: str, tree: Worktree, home: Path) -> list[LineEndingPlan]:
    plans: list[LineEndingPlan] = []
    asyncio.run(
        apply_patch(
            diff, tree.path, attempt_id="01A", env=ENV, home=home, on_normalized=plans.append
        )
    )
    return plans


def failure(diff: str, tree: Worktree, home: Path) -> PatchError:
    with pytest.raises(PatchError) as caught:
        apply(diff, tree, home)
    return caught.value


# Classification and .gitattributes


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"a\r\nb\r\n", "crlf"),
        (b"a\nb\n", "lf"),
        (b"a\r\nb\n", "mixed"),
        (b"no break", "lf"),
        (b"", "lf"),
        (b"x\r\n" * 19 + b"y\n", "crlf"),  # exactly 95%
        (b"x\r\n" * 18 + b"y\n" * 2, "mixed"),  # 90%
        (b"x\n" * 19 + b"y\r\n", "lf"),
    ],
)
def test_classify_line_endings(data: bytes, expected: str) -> None:
    assert classify_line_endings(data) == expected


@pytest.mark.parametrize(
    ("attributes", "path", "expected"),
    [
        (None, "run.bat", "lf"),
        ("*.bat eol=crlf\n", "run.bat", "crlf"),
        ("*.bat eol=crlf\n", "scripts/deep/run.bat", "crlf"),
        ("*.bat eol=crlf\n", "run.sh", "lf"),
        ("* text=auto eol=crlf\n", "any/file.txt", "crlf"),
        ("/docs/a.txt eol=crlf\n", "docs/a.txt", "crlf"),
        ("docs/a.txt eol=crlf\n", "docs/b.txt", "lf"),
        ("* eol=crlf\n*.sh eol=lf\n", "x.sh", "lf"),  # the last match wins
        ("*.bat eol=crlf\n*.bat -text\n", "x.bat", "lf"),
        ("# *.bat eol=crlf\n", "x.bat", "lf"),
        ("src/*.bat eol=crlf\n", "src/x.bat", "lf"),  # unsupported glob: ignored
    ],
)
def test_new_file_ending_reads_gitattributes(
    tmp_path: Path, attributes: str | None, path: str, expected: str
) -> None:
    if attributes is not None:
        (tmp_path / ".gitattributes").write_text(attributes)
    assert new_file_ending(tmp_path, path) == expected


# Normalization


def test_crlf_normalization_keeps_headers_and_no_newline_lines(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\r\n\r\ntwo")
    diff = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n\n-two\n"
        "\\ No newline at end of file\n+three\n\\ No newline at end of file\n"
    )
    plan = normalize_line_endings(diff, tmp_path)
    assert plan.diff == (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\r\n \r\n-two\n"
        "\\ No newline at end of file\n+three\n\\ No newline at end of file\n"
    )
    assert plan.expected == {"a.txt": "crlf"} and plan.mixed == []


def test_lf_normalization_strips_stray_carriage_returns(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\ntwo\n")
    diff = "--- a/a.txt\r\n+++ b/a.txt\r\n@@ -1,2 +1,2 @@\r\n one\r\n-two\r\n+TWO\r\n"
    plan = normalize_line_endings(diff, tmp_path)
    assert plan.diff == "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"


def test_mixed_file_hunks_are_left_alone(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"one\r\ntwo\n")
    diff = "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\r\n-two\n+TWO\n"
    plan = normalize_line_endings(diff, tmp_path)
    assert plan.diff == diff and plan.mixed == ["a.txt"]


def test_parse_diff_splits_git_and_plain_sections() -> None:
    diff = (
        "diff --git a/x.py b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/x.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n"
        "--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-[REDACTED:aws_access_key]\n+y = 2\n"
        "diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py\n"
    )
    parsed = parse_diff(diff)
    assert [(s.old, s.new) for s in parsed.sections] == [
        (None, "x.py"),
        ("y.py", "y.py"),
        ("old.py", "new.py"),
    ]
    assert redacted_hunk_paths(parsed) == ["y.py"]


# Applying (C1 to C4)


def test_crlf_file_with_an_lf_diff_applies_and_stays_crlf(tmp_path: Path, home: Path) -> None:
    tree = worktree(tmp_path, home, {OPS: CRLF_OPS})
    [plan] = apply(extract_patch(FIX), tree, home)
    assert plan.expected == {OPS: "crlf"}
    assert (tree.path / OPS).read_bytes() == b"def add(a, b):\r\n    return a + b\r\n"


def test_lf_file_with_stray_carriage_returns_applies_and_stays_lf(
    tmp_path: Path, home: Path
) -> None:
    tree = worktree(tmp_path, home, {})
    diff = extract_patch(FIX).replace("\n", "\r\n")
    assert "\r\n" in diff
    apply(diff, tree, home)
    assert (tree.path / OPS).read_bytes() == b"def add(a, b):\n    return a + b\n"


def test_mixed_file_with_a_non_applying_diff_reports_mixed_line_endings(
    tmp_path: Path, home: Path
) -> None:
    tree = worktree(tmp_path, home, {OPS: "def add(a, b):\r\n    return a - b\n"})
    error = failure(extract_patch(FIX), tree, home)
    assert error.reason == "patch_mixed_line_endings"
    assert error.detail.startswith(f"{OPS} mixed CRLF and LF line endings:")


def test_mixed_file_with_an_applying_diff_is_applied(tmp_path: Path, home: Path) -> None:
    tree = worktree(tmp_path, home, {OPS: "def add(a, b):\n    return a - b\r\n# end\n"})
    # The diff matches the file byte for byte, so it applies without normalization.
    diff = (
        "--- a/src/calc/ops.py\n+++ b/src/calc/ops.py\n@@ -1,2 +1,2 @@\n"
        "-def add(a, b):\n+def add(b, a):\n     return a - b\r\n"
    )
    apply(diff, tree, home)
    assert (tree.path / OPS).read_bytes() == b"def add(b, a):\n    return a - b\r\n# end\n"


def test_new_files_follow_gitattributes(tmp_path: Path, home: Path) -> None:
    tree = worktree(tmp_path, home, {".gitattributes": "*.bat eol=crlf\n"})
    diff = (
        "--- /dev/null\n+++ b/run.bat\n@@ -0,0 +1,2 @@\n+@echo off\n+echo hi\n"
        "--- /dev/null\n+++ b/notes.txt\n@@ -0,0 +1,2 @@\n+one\n+two\n"
    )
    [plan] = apply(diff, tree, home)
    assert plan.expected == {"run.bat": "crlf", "notes.txt": "lf"}
    assert (tree.path / "run.bat").read_bytes() == b"@echo off\r\necho hi\r\n"
    assert (tree.path / "notes.txt").read_bytes() == b"one\ntwo\n"


def test_changed_line_endings_after_apply_fail_the_attempt(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arpeggio_ai.orchestrator import patch as patch_module

    def wrong_plan(diff: str, root: Path) -> LineEndingPlan:
        # Expects CRLF but leaves the lines LF: the bug the post-apply check exists for.
        return LineEndingPlan(diff, {"run.bat": "crlf"}, [])

    monkeypatch.setattr(patch_module, "normalize_line_endings", wrong_plan)
    tree = worktree(tmp_path, home, {})
    diff = "--- /dev/null\n+++ b/run.bat\n@@ -0,0 +1,2 @@\n+@echo off\n+exit\n"
    error = failure(diff, tree, home)
    assert error.reason == "patch_line_endings_changed"
    assert error.detail == "run.bat was crlf and is lf after applying"


# Stored diffs (C5)


def test_raw_and_normalized_diffs_are_both_stored(tmp_path: Path, home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    repo = make_repo(tmp_path / "repo", {**FILES, OPS: CRLF_OPS})
    conn = open_db(db_path(home))
    try:
        artifacts = ArtifactStore(artifacts_dir(home))
        task = create_task(conn, register_repo(conn, repo).id, "Fix", "Fix add.", profile="micro")
        add_criterion(conn, task.id, {"kind": "file_exists", "path": OPS})
        context = make_context(
            template_config(), FakeProvider(ok(FIX)), prompt_overhead=overhead_history(conn)
        )
        outcome = asyncio.run(
            run_patch_attempt(
                conn,
                artifacts,
                context,
                task_id=task.id,
                route=Route("api", "tier1.flash", "low"),
                home=home,
                max_tokens=256,
                context_files=[OPS],
            )
        )
        assert outcome.task_status == "awaiting_review"
        folder = artifacts_dir(home) / task.id / str(outcome.attempt_id)
        raw = (folder / "patch-raw.diff").read_bytes()
        applied = (folder / "patch.diff").read_bytes()
        assert raw == extract_patch(FIX).encode()
        assert b"\r\n" not in raw
        assert applied == raw.replace(b" def add(a, b):\n", b" def add(a, b):\r\n").replace(
            b"    return a - b\n", b"    return a - b\r\n"
        ).replace(b"    return a + b\n", b"    return a + b\r\n")
    finally:
        conn.close()
