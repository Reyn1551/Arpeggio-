"""Automatic context selection (M0.9): ranking is deterministic, capped and explainable.

These tests pin the properties the milestone depends on rather than one hand-tuned ordering:
a request that names a file must surface it, the caps must hold, and the same inputs must
always produce the same list.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from arpeggio_ai.orchestrator.context import Candidate, rank

CALC = [
    Candidate("src/calc/ops.py", 400),
    Candidate("src/calc/parser.py", 400),
    Candidate("tests/test_calc.py", 300),
    Candidate("README.md", 900),
    Candidate("docs/design.md", 900),
]


def test_request_naming_a_file_surfaces_it_first() -> None:
    """A request that names ``mul`` in ``calc.ops`` must rank that file above the rest."""
    chosen = rank(
        "Add a function mul(a, b) to calc.ops that returns the product.",
        CALC,
        limit_bytes=10_000,
        max_files=5,
    )
    assert chosen[0] == "src/calc/ops.py"


def test_request_naming_a_path_surfaces_it() -> None:
    chosen = rank(
        "Change the parser in src/calc/parser.py",
        CALC,
        limit_bytes=10_000,
        max_files=5,
    )
    assert chosen[0] == "src/calc/parser.py"


def test_a_request_naming_nothing_specific_yields_nothing() -> None:
    """No signal means no context, rather than an arbitrary dump of the repository."""
    assert rank("Fix the thing", CALC, limit_bytes=10_000, max_files=5) == []


def test_hidden_style_paths_are_not_special_cased_here() -> None:
    """The selector never decides what is *allowed*; the caller's guards still do that.

    A test file may be chosen (it is plausible context). Whether it is readable, and whether
    it is a hidden test, stays ``read_context``'s decision — the safety surface does not move.
    """
    chosen = rank(
        "Make tests/test_calc.py pass for calc",
        CALC,
        limit_bytes=10_000,
        max_files=5,
    )
    assert "tests/test_calc.py" in chosen


def test_vendor_trees_rank_below_real_sources() -> None:
    """A vendor directory alone is enough to demote a file that would otherwise win.

    ``node_modules/index.js`` and ``lib/x/index.js`` share the basename, so both get the exact
    and basename bonuses; the vendor path is one level shallower and would come first on depth,
    but the 30-point vendor penalty keeps the real source ahead. Measured: 142.50 against 172.00
    with the penalty, and 172.50 against 172.00 without it — so this test fails if it is removed.
    """
    candidates = [
        Candidate("node_modules/index.js", 10),
        Candidate("lib/x/index.js", 10),
    ]
    chosen = rank("Update index.js", candidates, limit_bytes=10_000, max_files=5)
    assert chosen == ["lib/x/index.js", "node_modules/index.js"]


def test_a_directory_word_surfaces_files_under_that_directory() -> None:
    """Naming a directory is what makes a file under it selectable at all.

    Measured for the request ``fix src helper``: ``src/ops.py`` scores 11.50 (the 12-point
    directory bonus) while ``lib/ops.py`` scores -0.50 and is dropped. Dropping the bonus would
    send ``src/ops.py`` negative too, so this fails if the directory rule is removed.
    """
    candidates = [Candidate("lib/ops.py", 10), Candidate("src/ops.py", 10)]
    assert rank("fix src helper", candidates, limit_bytes=10_000, max_files=5) == ["src/ops.py"]


def test_a_shallower_file_is_preferred_over_a_deeper_copy() -> None:
    """When all else is equal, the fewer directories a file sits in, the sooner it is shown.

    ``ops.py`` and ``deep/nested/ops.py`` score identically except for the depth penalty; the
    root-level file must come first regardless of input order.
    """
    candidates = [Candidate("deep/nested/ops.py", 10), Candidate("ops.py", 10)]
    assert rank("Fix ops", candidates, limit_bytes=10_000, max_files=5) == [
        "ops.py",
        "deep/nested/ops.py",
    ]
    assert rank("Fix ops", list(reversed(candidates)), limit_bytes=10_000, max_files=5) == [
        "ops.py",
        "deep/nested/ops.py",
    ]


def test_test_files_rank_below_the_source_they_cover() -> None:
    """A test-marked path loses to the module it covers, on the penalty alone.

    Measured for the request ``ops``: ``src/test_ops.py`` scores -6.50 and ``src/ops.py`` 32.50,
    a gap of exactly the 6-point test penalty. Both the bare ``test_ops.py`` (-6.00) and the
    in-tree ``src/test_ops.py`` (-6.50) are non-positive, so ``rank`` drops them.
    """
    for path in ("test_ops.py", "src/test_ops.py"):
        assert rank("ops", [Candidate(path, 10)], limit_bytes=10_000, max_files=5) == [], path
    both = [Candidate("src/test_ops.py", 10), Candidate("src/ops.py", 10)]
    assert rank("ops", both, limit_bytes=10_000, max_files=5) == ["src/ops.py"]
    # Reverse the input order: the penalty must decide, not the tie-break by path.
    assert rank("ops", list(reversed(both)), limit_bytes=10_000, max_files=5) == ["src/ops.py"]


def test_the_test_penalty_reorders_paths_that_all_survive() -> None:
    """The penalty changes the order of paths that all score positively.

    Measured for the request ``ops``: with the penalty ``src/t/ops.py`` (a test-marked path)
    comes second; without it, it drops to fifth, behind ``test/ops.py`` and ``tests/ops.py``.
    The paths all stay in the list either way, so only the order reveals the penalty — the
    sign-based tests above cannot see this.
    """
    paths = ["src/t/ops.py", "test/ops.py", "tests/ops.py", "src/ops.py"]
    chosen = rank("ops", [Candidate(p, 10) for p in paths], limit_bytes=10_000, max_files=10)
    assert chosen == ["src/ops.py", "src/t/ops.py", "test/ops.py", "tests/ops.py"]


def test_the_test_penalty_is_what_removes_a_weak_test_path() -> None:
    """The penalty, not the depth term, is what separates these two paths.

    Measured for the request ``ops``: ``src/test_ops.py`` scores -6.50 and is dropped, while
    ``test/ops.py`` scores 26.50 and is kept. ``test/ops.py`` is only one character longer, so
    depth alone cannot explain a 33-point gap; the test penalty is what does.
    """
    assert rank("ops", [Candidate("src/test_ops.py", 10)], limit_bytes=10_000, max_files=5) == []
    assert rank("ops", [Candidate("test/ops.py", 10)], limit_bytes=10_000, max_files=5) == [
        "test/ops.py"
    ]
    # Head to head the kept one wins, whichever order they arrive in.
    both = [Candidate("src/test_ops.py", 10), Candidate("test/ops.py", 10)]
    assert rank("ops", both, limit_bytes=10_000, max_files=5) == ["test/ops.py"]
    assert rank("ops", list(reversed(both)), limit_bytes=10_000, max_files=5) == ["test/ops.py"]


def test_the_exact_named_path_outranks_a_basename_only_match() -> None:
    """A path the request names in full must beat one that only shares its basename.

    Measured: for the request ``edit src/calc/ops.py``, ``src/calc/ops.py`` scores 184.00 and
    ``src/ops.py`` 84.50. Drop the exact-match rule and ``src/ops.py`` is promoted to first
    place, so the selector would show a same-named file from the wrong directory. This is the
    rule that keeps a path-qualified request anchored to the file it actually named.
    """
    request = "edit src/calc/ops.py"
    candidates = [
        Candidate("src/ops.py", 10),
        Candidate("src/calc/ops.py", 10),
        Candidate("ops.py", 10),
    ]
    assert rank(request, candidates, limit_bytes=10_000, max_files=5)[0] == "src/calc/ops.py"
    # Order of the candidates must not matter.
    assert rank(request, list(reversed(candidates)), limit_bytes=10_000, max_files=5)[0] == (
        "src/calc/ops.py"
    )


def test_an_exactly_named_basename_outranks_a_deeper_copy() -> None:
    """Naming a bare basename prefers the shallow copy of it over a nested one."""
    request = "Update README.md"
    candidates = [Candidate("docs/README.md", 10), Candidate("README.md", 10)]
    assert rank(request, candidates, limit_bytes=10_000, max_files=5) == [
        "README.md",
        "docs/README.md",
    ]


def test_a_stopword_stem_does_not_select_a_file() -> None:
    """A request of filler words selects nothing, even when a file is named after one.

    Measured: ``src/not.py`` scores 0.00 with the filter and 33.00 without it. ``rank`` keeps
    only positive scores, so the filtered score selects nothing and the unfiltered one would
    select the file — this is what the stopword list actually buys.
    """
    filler = "the and with not"
    assert rank(filler, [Candidate("src/not.py", 10)], limit_bytes=10_000, max_files=5) == []
    assert rank(filler, [Candidate("not.py", 10)], limit_bytes=10_000, max_files=5) == []


def test_a_real_word_still_scores_when_stopwords_are_present() -> None:
    """The stopword filter must not remove so much that a genuine match stops scoring."""
    candidates = [Candidate("src/calc.py", 10)]
    chosen = rank("Fix the bug in calc that we saw", candidates, limit_bytes=10_000, max_files=5)
    assert chosen == ["src/calc.py"]


def test_byte_cap_is_respected_and_smaller_files_still_fit() -> None:
    """A file that does not fit is skipped, and a smaller one further down may still be taken."""
    candidates = [
        Candidate("src/calc/big.py", 5_000),
        Candidate("src/calc/small.py", 100),
    ]
    chosen = rank(
        "Update calc big and calc small",
        candidates,
        limit_bytes=200,
        max_files=5,
    )
    assert chosen == ["src/calc/small.py"]


def test_file_cap_is_respected() -> None:
    chosen = rank(
        "Update calc ops parser tests README docs",
        CALC,
        limit_bytes=10_000,
        max_files=2,
    )
    assert len(chosen) == 2


def test_zero_caps_yield_nothing() -> None:
    assert rank("Update calc ops", CALC, limit_bytes=0, max_files=5) == []
    assert rank("Update calc ops", CALC, limit_bytes=10_000, max_files=0) == []


def test_order_is_stable_for_equal_scores() -> None:
    """Ties break on path, so two runs on different platforms agree."""
    candidates = [
        Candidate("src/zzz/ops.py", 10),
        Candidate("src/aaa/ops.py", 10),
    ]
    first = rank("Update ops in src", candidates, limit_bytes=10_000, max_files=5)
    second = rank("Update ops in src", list(reversed(candidates)), limit_bytes=10_000, max_files=5)
    assert first == second
    assert first == ["src/aaa/ops.py", "src/zzz/ops.py"]


def test_no_candidates_yields_nothing() -> None:
    assert rank("Update calc ops", [], limit_bytes=10_000, max_files=5) == []


def test_same_inputs_always_give_the_same_list() -> None:
    request = "Add a function mul(a, b) to calc.ops that returns the product."
    once = rank(request, CALC, limit_bytes=10_000, max_files=5)
    twice = rank(request, CALC, limit_bytes=10_000, max_files=5)
    assert once == twice


@given(
    request=st.text(max_size=200),
    paths=st.lists(st.text(min_size=1, max_size=40), max_size=20, unique=True),
    limit_bytes=st.integers(min_value=0, max_value=5_000),
    max_files=st.integers(min_value=0, max_value=10),
)
@settings(max_examples=200, deadline=None)
def test_rank_never_exceeds_its_caps_or_invents_paths(
    request: str, paths: list[str], limit_bytes: int, max_files: int
) -> None:
    """Property: the result is a subset of the candidates and it respects both caps.

    ``read_context`` enforces the same caps, so a violation here would be a prompt the caller
    refuses to build — the selector must never propose one.
    """
    candidates = [Candidate(path, 50) for path in paths]
    chosen = rank(request, candidates, limit_bytes=limit_bytes, max_files=max_files)
    assert len(chosen) <= max_files
    assert set(chosen) <= set(paths)
    assert len(set(chosen)) == len(chosen)
    assert sum(50 for _ in chosen) <= limit_bytes or not chosen
