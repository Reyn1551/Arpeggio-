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
    candidates = [
        Candidate("node_modules/pkg/index.js", 10),
        Candidate("src/app/index.js", 10),
    ]
    chosen = rank("Update index.js in app", candidates, limit_bytes=10_000, max_files=5)
    assert chosen[0] == "src/app/index.js"


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
