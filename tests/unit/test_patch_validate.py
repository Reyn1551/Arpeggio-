from pathlib import Path

import pytest

from arpeggio_ai.orchestrator.patch import (
    MAX_PATCH_BYTES,
    PatchError,
    build_prompt,
    extract_patch,
    system_prompt,
    validate_patch,
)

SIMPLE = """diff --git a/src/calc/ops.py b/src/calc/ops.py
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def fenced(body: str, info: str = "diff", fence: str = "```") -> str:
    return f"{fence}{info}\n{body}{fence}\n"


def reason(diff: str, worktree: Path) -> tuple[str, str]:
    with pytest.raises(PatchError) as caught:
        validate_patch(diff, worktree)
    return caught.value.reason, caught.value.detail


# Extraction


@pytest.mark.parametrize("info", ["diff", "patch", "DIFF", "diff title=fix"])
def test_extracts_the_one_block(info: str) -> None:
    reply = "Here is the fix.\n\n" + fenced(SIMPLE, info) + "\nDone."
    assert extract_patch(reply) == SIMPLE


def test_tilde_fences_and_other_blocks_are_fine() -> None:
    reply = fenced("print('x')\n", "python") + fenced(SIMPLE, "diff", "~~~~")
    assert extract_patch(reply) == SIMPLE


def test_inner_backticks_do_not_close_a_longer_fence() -> None:
    body = SIMPLE + "+```\n"
    assert extract_patch(fenced(body, "diff", "````")) == body


def test_no_diff_block_is_patch_missing() -> None:
    for reply in ("I fixed it.", fenced("print(1)\n", "python"), fenced(SIMPLE, "")):
        with pytest.raises(PatchError) as caught:
            extract_patch(reply)
        assert caught.value.reason == "patch_missing"


def test_two_diff_blocks_are_patch_ambiguous() -> None:
    with pytest.raises(PatchError) as caught:
        extract_patch(fenced(SIMPLE) + fenced(SIMPLE, "patch"))
    assert (caught.value.reason, caught.value.detail) == (
        "patch_ambiguous",
        "the reply has 2 diff blocks; expected one",
    )


def test_unclosed_block_still_counts() -> None:
    assert extract_patch("```diff\n" + SIMPLE) == SIMPLE


# Validation


def test_valid_diff_declares_its_paths(tmp_path: Path) -> None:
    assert validate_patch(SIMPLE, tmp_path) == ["src/calc/ops.py"]


def test_new_deleted_and_renamed_files(tmp_path: Path) -> None:
    diff = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-y = 2\n"
        "diff --git a/a.py b/b.py\nsimilarity index 100%\nrename from a.py\nrename to b.py\n"
    )
    assert validate_patch(diff, tmp_path) == ["a.py", "b.py", "new.py", "old.py"]


def test_hunk_lines_that_look_like_headers_are_content(tmp_path: Path) -> None:
    diff = (
        "--- a/notes.md\n+++ b/notes.md\n@@ -1,2 +1,2 @@\n line\n"
        "---- not a header\n+++++ also content\n"
    )
    assert validate_patch(diff, tmp_path) == ["notes.md"]


@pytest.mark.parametrize(
    ("header", "path"),
    [
        ("--- a/../outside.py\n+++ b/../outside.py\n", "../outside.py"),
        ("--- a/src/../../x.py\n+++ b/src/../../x.py\n", "src/../../x.py"),
        ("--- a/.git/config\n+++ b/.git/config\n", ".git/config"),
        (
            "--- a/sub/.git/hooks/pre-commit\n+++ b/sub/.git/hooks/pre-commit\n",
            "sub/.git/hooks/pre-commit",
        ),
        ("--- a/.GIT/config\n+++ b/.GIT/config\n", ".GIT/config"),
        ("--- /etc/passwd\n+++ /etc/passwd\n", "/etc/passwd"),
        ("--- C:/Windows/win.ini\n+++ C:/Windows/win.ini\n", "C:/Windows/win.ini"),
        ("--- a\\x.py\n+++ a\\x.py\n", "a\\x.py"),
    ],
)
def test_unsafe_paths(tmp_path: Path, header: str, path: str) -> None:
    got_reason, detail = reason(header + "@@ -1 +1 @@\n-a\n+b\n", tmp_path)
    assert got_reason == "patch_unsafe"
    assert detail.startswith(f"{path}: ")


def test_unsafe_rename_target(tmp_path: Path) -> None:
    diff = "diff --git a/a.py b/a.py\nrename from a.py\nrename to ../a.py\n"
    assert reason(diff, tmp_path)[1].startswith("../a.py: ")


def test_quoted_paths_are_refused(tmp_path: Path) -> None:
    diff = 'diff --git "a/sp ace.py" "b/sp ace.py"\n--- "a/sp ace.py"\n+++ "b/sp ace.py"\n'
    assert reason(diff, tmp_path) == (
        "patch_unsafe",
        'quoted paths are not supported: "a/sp ace.py" "b/sp ace.py"',
    )


def test_binary_patches_are_refused(tmp_path: Path) -> None:
    for body in (
        "diff --git a/x.bin b/x.bin\nGIT binary patch\nliteral 3\nKcmZ?\n",
        "diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n",
    ):
        assert reason(body, tmp_path) == ("patch_unsafe", "binary patches are not allowed")


@pytest.mark.parametrize(
    "mode_line",
    [
        "new file mode 120000",
        "old mode 120000",
        "new mode 120000",
        "deleted file mode 120000",
        "index 1234567..89abcde 120000",
    ],
)
def test_symlinks_are_refused(tmp_path: Path, mode_line: str) -> None:
    diff = (
        f"diff --git a/link b/link\n{mode_line}\n--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+/etc\n"
    )
    assert reason(diff, tmp_path) == ("patch_unsafe", "symlinks are not allowed")


def test_oversize_diff_is_refused(tmp_path: Path) -> None:
    body = "--- a/big.txt\n+++ b/big.txt\n@@ -0,0 +1 @@\n+" + "x" * MAX_PATCH_BYTES + "\n"
    got_reason, detail = reason(body, tmp_path)
    assert got_reason == "patch_unsafe" and "204800-byte limit" in detail


def test_diff_without_files(tmp_path: Path) -> None:
    assert reason("just some text\n", tmp_path)[0] == "patch_does_not_apply"


# Prompt


def test_system_prompt_is_packaged() -> None:
    text = system_prompt()
    assert "exactly one fenced code block" in text and "```diff" in text


def test_build_prompt() -> None:
    prompt = build_prompt(
        "  Fix add.  ",
        ["The command `pytest` exits with code 0."],
        [("src/calc/ops.py", "def add(a, b):\n    return a - b\n")],
    )
    assert prompt.startswith("Task:\nFix add.\n\nChecks that must pass")
    assert "- The command `pytest` exits with code 0." in prompt
    assert "File `src/calc/ops.py`:\n````\ndef add(a, b):" in prompt
    assert "Files from the repository" not in build_prompt("x", [], [])
