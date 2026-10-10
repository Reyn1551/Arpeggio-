"""Search/replace edit format (M0.8): parsing, exact matching and line breaks."""

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arpeggio_ai.orchestrator.edits import (
    Edit,
    edit_file,
    parse_edits,
    redacted_replacements,
    split_lines,
)
from arpeggio_ai.orchestrator.patch import PatchError, build_prompt, system_prompt

# patch_system.txt at b1332e2 (M0.7): the udiff baseline must not change in M0.8.
UDIFF_PROMPT_SHA256 = "a17c2c01fe627eac41402ca3b650f1394da2319a80838bc5abbceadcc516ce57"

FIX = """Here is the fix.

FILE: src/calc/ops.py
<<<<<<< SEARCH
    return a - b
=======
    return a + b
>>>>>>> REPLACE
"""


def reason(reply: str) -> tuple[str, str]:
    with pytest.raises(PatchError) as caught:
        parse_edits(reply)
    return caught.value.reason, caught.value.detail


def block(path: str, search: str, replace: str, header: str = "FILE") -> str:
    return f"{header}: {path}\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n"


def edit(search: list[str], replace: list[str], number: int = 1) -> Edit:
    return Edit("f.py", False, tuple(search), tuple(replace), number)


# Prompts


def test_udiff_prompt_is_unchanged_from_m07() -> None:
    text = system_prompt("udiff")
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == UDIFF_PROMPT_SHA256
    assert system_prompt() == text


def test_search_replace_prompt_is_packaged() -> None:
    text = system_prompt("search-replace")
    assert "<<<<<<< SEARCH" in text and ">>>>>>> REPLACE" in text and "NEW FILE:" in text
    assert "```diff" not in text


def test_build_prompt_wording_per_format() -> None:
    udiff = build_prompt("Fix add.", ["a"], [])
    assert "Checks that must pass after your diff is applied:" in udiff
    assert build_prompt("Fix add.", ["a"], [], edit_format="udiff") == udiff
    edits = build_prompt("Fix add.", ["a"], [], edit_format="search-replace")
    assert edits == udiff.replace("your diff is applied", "your edits are applied")


# Parsing


def test_parse_one_block_ignores_prose() -> None:
    [only] = parse_edits(FIX + "\nThat is all.\n")
    assert only == Edit("src/calc/ops.py", False, ("    return a - b",), ("    return a + b",), 1)


def test_parse_keeps_whitespace_and_strips_carriage_returns() -> None:
    reply = block("a.py", "  x = 1  \n\n", "\ty = 2\n").replace("\n", "\r\n")
    [only] = parse_edits(reply)
    assert only.search == ("  x = 1  ", "") and only.replace == ("\ty = 2",)


def test_parse_skips_one_fence_line_after_the_header() -> None:
    reply = "FILE: a.py\n```python\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n```\n"
    assert parse_edits(reply)[0].search == ("x",)


def test_parse_two_fence_lines_are_malformed() -> None:
    reply = "FILE: a.py\n```\n```\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n"
    assert reason(reply)[0] == "patch_malformed"


def test_parse_new_file_and_empty_replace() -> None:
    reply = block("new.py", "", "VALUE = 1\n", header="NEW FILE") + block("a.py", "x\n", "")
    new, delete = parse_edits(reply)
    assert (new.new, new.search, new.replace, new.number) == (True, (), ("VALUE = 1",), 1)
    assert (delete.new, delete.replace, delete.number) == (False, (), 2)


def test_no_block_is_patch_missing() -> None:
    assert reason("I changed it.")[0] == "patch_missing"
    assert reason("```diff\n--- a/x\n+++ b/x\n```")[0] == "patch_missing"


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        ("<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n", "outside a FILE: block"),
        ("FILE: a.py\nx\n", "is not followed by <<<<<<< SEARCH"),
        ("FILE:\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n", "without a path"),
        ("FILE: a.py\n<<<<<<< SEARCH\nx\n=======\ny\n", "is not closed"),
        ("FILE: a.py\n<<<<<<< SEARCH\nx\n>>>>>>> REPLACE\n", "unexpected >>>>>>> REPLACE"),
        (
            "FILE: a.py\n<<<<<<< SEARCH\nx\n=======\ny\n=======\nz\n>>>>>>> REPLACE\n",
            "unexpected =======",
        ),
        (block("a.py", "", "y\n"), "has an empty SEARCH"),
        (block("a.py", "x\n", "y\n", header="NEW FILE"), "needs an empty SEARCH"),
        (block("a.py", "x\n", "x\n"), "changes nothing"),
    ],
)
def test_malformed_blocks(reply: str, detail: str) -> None:
    got, text = reason(reply)
    assert got == "patch_malformed" and detail in text


def test_new_file_declared_twice_or_also_edited_is_ambiguous() -> None:
    twice = block("n.py", "", "a\n", header="NEW FILE") * 2
    assert reason(twice)[0] == "patch_ambiguous"
    mixed = block("n.py", "", "a\n", header="NEW FILE") + block("n.py", "a\n", "b\n")
    assert reason(mixed)[0] == "patch_ambiguous"


def test_redacted_replacements_name_places_only() -> None:
    edits = parse_edits(block("a.py", "x\n", "key = '[REDACTED:api_key]'\n"))
    assert redacted_replacements(edits) == ["a.py:block 1 line 1"]
    assert redacted_replacements(parse_edits(block("a.py", "[REDACTED:x]\n", "y\n"))) == []


# Lines and matching


def test_split_lines() -> None:
    assert split_lines("a\r\nb\nc") == [("a", "\r\n"), ("b", "\n"), ("c", "")]
    assert split_lines("a\n") == [("a", "\n")]
    assert split_lines("") == []
    assert split_lines("a\fb\u2028c\n") == [("a\fb\u2028c", "\n")]


def test_edit_file_replaces_the_one_match() -> None:
    text = "def add(a, b):\n    return a - b\n"
    out = edit_file("f.py", text, "lf", [edit(["    return a - b"], ["    return a + b"])])
    assert out == "def add(a, b):\n    return a + b\n"


def test_match_is_exact_and_reports_whitespace_near_miss() -> None:
    with pytest.raises(PatchError) as caught:
        edit_file("f.py", "x = 1  \ny\n", "lf", [edit(["x = 1"], ["x = 2"])])
    assert caught.value.reason == "patch_search_not_found"
    assert "block 1 matches nothing (whitespace_near_miss)" in caught.value.detail
    with pytest.raises(PatchError) as caught:
        edit_file("f.py", "x = 1\n", "lf", [edit(["z"], ["x = 2"])])
    assert "whitespace_near_miss" not in caught.value.detail


def test_indentation_is_not_ignored() -> None:
    with pytest.raises(PatchError) as caught:
        edit_file("f.py", "\treturn 1\n", "lf", [edit(["    return 1"], ["    return 2"])])
    assert caught.value.reason == "patch_search_not_found"


def test_several_matches_are_ambiguous() -> None:
    with pytest.raises(PatchError) as caught:
        edit_file("f.py", "pass\nx\npass\n", "lf", [edit(["pass"], ["return"])])
    assert caught.value.reason == "patch_search_ambiguous"
    assert "matches 2 places" in caught.value.detail


def test_blocks_match_the_original_not_each_other() -> None:
    text = "a\nb\nc\n"
    out = edit_file("f.py", text, "lf", [edit(["c"], ["C"], 1), edit(["a"], ["A1", "A2"], 2)])
    assert out == "A1\nA2\nb\nC\n"
    with pytest.raises(PatchError) as caught:  # the second SEARCH needs the first's result
        edit_file("f.py", text, "lf", [edit(["a"], ["z"], 1), edit(["z"], ["y"], 2)])
    assert caught.value.reason == "patch_search_not_found"


@pytest.mark.parametrize(
    ("first", "second"), [(["a", "b"], ["b", "c"]), (["b"], ["b"]), (["a", "b", "c"], ["b"])]
)
def test_overlapping_blocks_are_rejected(first: list[str], second: list[str]) -> None:
    with pytest.raises(PatchError) as caught:
        edit_file("f.py", "a\nb\nc\n", "lf", [edit(first, ["x"], 1), edit(second, ["y"], 2)])
    assert caught.value.reason == "patch_edit_overlap"
    assert "blocks 1 and 2" in caught.value.detail


def test_adjacent_blocks_are_not_overlapping() -> None:
    out = edit_file("f.py", "a\nb\n", "lf", [edit(["a"], ["x"], 1), edit(["b"], ["y"], 2)])
    assert out == "x\ny\n"


def test_crlf_lines_keep_crlf_and_the_rest_is_untouched() -> None:
    text = "a\r\nb\r\nc\n"  # c's bare LF stays bare
    out = edit_file("f.py", text, "crlf", [edit(["b"], ["B1", "B2"])])
    assert out == "a\r\nB1\r\nB2\r\nc\n"


def test_last_line_without_newline_stays_without() -> None:
    out = edit_file("f.py", "a\nb", "lf", [edit(["b"], ["B", "C"])])
    assert out == "a\nB\nC"
    out = edit_file("f.py", "a\r\nb", "crlf", [edit(["b"], ["B", "C"])])
    assert out == "a\r\nB\r\nC"


def test_empty_replace_removes_lines() -> None:
    assert edit_file("f.py", "a\nb\nc\n", "lf", [edit(["b"], [])]) == "a\nc\n"


LINE = st.text(alphabet="ab \t", max_size=4)


@given(
    lines=st.lists(st.tuples(LINE, st.sampled_from(["\n", "\r\n"])), min_size=1, max_size=12),
    data=st.data(),
)
def test_bytes_outside_the_match_never_change(
    lines: list[tuple[str, str]], data: st.DataObject
) -> None:
    contents = [content for content, _ in lines]
    start = data.draw(st.integers(0, len(lines) - 1))
    end = data.draw(st.integers(start + 1, len(lines)))
    search = contents[start:end]
    count = sum(contents[i : i + len(search)] == search for i in range(len(contents)))
    text = "".join(content + brk for content, brk in lines)
    replace = data.draw(st.lists(st.text(alphabet="xy", min_size=1, max_size=3), max_size=3))
    if count != 1:
        with pytest.raises(PatchError) as caught:
            edit_file("f.py", text, "lf", [edit(search, replace)])
        assert caught.value.reason == "patch_search_ambiguous"
        return
    out = edit_file("f.py", text, "lf", [edit(search, replace)])
    head = "".join(content + brk for content, brk in lines[:start])
    tail = "".join(content + brk for content, brk in lines[end:])
    assert out.startswith(head) and out.endswith(tail)
    middle = out[len(head) : len(out) - len(tail)]
    assert [content for content, _ in split_lines(middle)] == replace
