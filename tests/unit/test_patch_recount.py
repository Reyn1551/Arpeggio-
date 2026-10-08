"""recount_hunks (M0.7): rewrite @@ counts from hunk bodies, never touch any other line."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arpeggio_ai.orchestrator.patch import PatchError, recount_hunks

GOOD = """diff --git a/src/calc/ops.py b/src/calc/ops.py
--- a/src/calc/ops.py
+++ b/src/calc/ops.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def wrong(diff: str, header: str) -> str:
    return diff.replace("@@ -1,2 +1,2 @@", header, 1)


@pytest.mark.parametrize(
    "diff",
    [
        GOOD,
        GOOD.replace("\n", "\r\n"),
        "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",  # counts left out mean 1
        "--- a/x\n+++ b/x\n@@ -3,2 +3,2 @@ def f():\n a\n-b\n+c\n\\ No newline at end of file\n",
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+a\n+b\n",
        "--- a/x\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n",
        GOOD + GOOD.replace("ops.py", "other.py"),
        "--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n",  # a blank context line
        GOOD + "\n\n",  # trailing blank lines after the last hunk
        GOOD + "That's the fix.\n",  # trailing prose after a complete hunk, as git allows
        "no hunks at all\n",
    ],
)
def test_a_correct_diff_comes_back_byte_for_byte(diff: str) -> None:
    assert recount_hunks(diff) == (diff, 0)


@pytest.mark.parametrize("header", ["@@ -1,3 +1,4 @@", "@@ -1 +1 @@", "@@ -1,9 +1,2 @@"])
def test_wrong_counts_are_rewritten_and_nothing_else(header: str) -> None:
    fixed, changed = recount_hunks(wrong(GOOD, header))
    assert (fixed, changed) == (GOOD, 1)


def test_the_section_heading_and_start_lines_are_kept() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -7,9 +8,1 @@ class A:\n a\n-b\n+c\n+d\n"
    fixed = diff.replace("@@ -7,9 +8,1 @@", "@@ -7,2 +8,3 @@")
    assert recount_hunks(diff) == (fixed, 1)


def test_a_crlf_header_keeps_its_carriage_return() -> None:
    fixed, changed = recount_hunks(wrong(GOOD, "@@ -1,5 +1,5 @@").replace("\n", "\r\n"))
    assert (fixed, changed) == (GOOD.replace("\n", "\r\n"), 1)


def test_only_wrong_hunks_count_as_recounted() -> None:
    diff = GOOD + wrong(GOOD.replace("ops.py", "other.py"), "@@ -1,1 +1,1 @@")
    assert recount_hunks(diff) == (GOOD + GOOD.replace("ops.py", "other.py"), 1)


def test_trailing_blank_lines_are_kept_but_not_counted() -> None:
    fixed, changed = recount_hunks(wrong(GOOD, "@@ -1,4 +1,4 @@") + "\n\n")
    assert (fixed, changed) == (GOOD + "\n\n", 1)


def test_a_header_pair_inside_the_declared_counts_is_body() -> None:
    # As in git: within the counts, "--- x" is a removed line and "+++ y" an added one.
    diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n a\n--- x\n+++ y\n"
    assert recount_hunks(diff) == (diff, 0)


def test_a_header_pair_past_the_declared_counts_starts_a_new_file() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n--- a/y\n+++ b/y\n@@ -1 +1 @@\n-c\n+d\n"
    assert recount_hunks(diff) == (diff, 0)


@pytest.mark.parametrize(
    "body", [" a\nnot a diff line\n-b\n+c\n", " a\n-b\nindex 1234..5678\n+c\n"]
)
def test_a_line_that_is_not_a_hunk_line_is_malformed(body: str) -> None:
    with pytest.raises(PatchError) as caught:
        recount_hunks(f"--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n{body}")
    assert caught.value.reason == "patch_malformed"
    assert "not a hunk line" in caught.value.detail


hunk_line = st.sampled_from([" ", "-", "+"]).flatmap(
    lambda marker: st.text("abc ", max_size=6).map(lambda text: marker + text)
)


@given(st.lists(hunk_line, min_size=1, max_size=20), st.integers(0, 30), st.integers(0, 30))
def test_any_counts_are_recounted_from_the_body(body: list[str], old: int, new: int) -> None:
    right_old = sum(line[0] in " -" for line in body)
    right_new = sum(line[0] in " +" for line in body)
    correct = f"--- a/x\n+++ b/x\n@@ -1,{right_old} +1,{right_new} @@\n" + "\n".join(body) + "\n"
    assert recount_hunks(correct) == (correct, 0)
    garbled = correct.replace(f"-1,{right_old} +1,{right_new}", f"-1,{old} +1,{new}", 1)
    fixed, changed = recount_hunks(garbled)
    assert fixed == correct
    assert changed == int((old, new) != (right_old, right_new))
    assert len(fixed.split("\n")) == len(garbled.split("\n"))
