"""Automatic context selection (M0.9): choose the files an attempt may modify.

M0.8 measured that the edit format is not what limits ``solved``. The next candidate is the
context: until now every file the model may touch had to be named by hand in a task's
``context_files``, so a task that forgot one failed as ``patch_unknown_path`` however good the
model was — a harness limitation wearing the costume of a model failure.

This module is the selection half only. It is **pure**: it takes the task request and a list of
candidate files that the caller read from the base commit (``git ls-files``), and returns the
paths to put in the prompt, in the order to show them. It reads no file, touches no worktree,
and never decides what is *allowed* — a path the selector did not choose is not forbidden, only
not shown (M0.7's rule is unchanged: unknown means absent from the base commit).

Scoring is rule-based and explainable, in the spirit of ADR-0003 (rules before learning): a
path scores on how its components and basename overlap the words of the request, with a strong
bonus when the request names the basename verbatim. Ties break on path, so the same inputs
always give the same order. Nothing here is a magic number: the weights are named constants.

The caps are enforced here as well as in ``read_context`` so the choice and the byte budget
cannot disagree: ``limit_bytes`` mirrors ``CONTEXT_CAP_BYTES`` and ``max_files`` bounds the
count. Files too large to fit are skipped rather than truncating the list.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

# Score weights. Named so a change is visible in review rather than a bare number in a formula.
BASENAME_EXACT = 100.0
BASENAME_IN_REQUEST = 40.0
STEM_IN_REQUEST = 25.0
DIR_IN_REQUEST = 12.0
PATH_IN_REQUEST = 8.0
# The extension of a named file matters less than its name, but "ops.py" should beat "ops.json".
EXTENSION_IN_REQUEST = 4.0
# A small penalty per path segment keeps a shallow file ahead of a deep one when scores tie.
DEPTH_PENALTY = 0.5
# Vendored, generated and dependency trees are rarely the answer and are large.
VENDOR_PENALTY = 30.0
# Test files are plausible context but rarely the file a task asks to change.
TEST_PENALTY = 6.0

VENDOR_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "vendor",
        "site-packages",
        "target",
    }
)
TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "tests/", "test/", "spec/")
# Words too common to carry signal. Kept short and obvious on purpose.
STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "for",
        "with",
        "that",
        "this",
        "it",
        "is",
        "are",
        "be",
        "must",
        "should",
        "when",
        "keep",
        "add",
        "new",
        "only",
        "not",
    }
)

_WORD = re.compile(r"[A-Za-z0-9_]+")
# Words that look like file names in the request: ``calc.ops``, ``ops.py``, ``src/calc/ops.py``.
_PATHISH = re.compile(r"[A-Za-z0-9_./-]*[/.][A-Za-z0-9_./-]+")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One file that may be shown to a model: its repo-relative path and size in bytes."""

    path: str
    size: int


@dataclass(frozen=True, slots=True)
class Scored:
    """A candidate with the score that ranked it, kept for logging and tests."""

    path: str
    score: float


def _words(text: str) -> set[str]:
    """Lowercase word tokens of ``text``, without the stopwords."""
    return {word.lower() for word in _WORD.findall(text)} - STOPWORDS


def _named_paths(request: str) -> set[str]:
    """Lowercase path-like tokens the request mentions, e.g. ``calc/ops.py`` or ``ops.py``."""
    return {
        token.lower().strip("/.")
        for token in _PATHISH.findall(request)
        if any(char in token for char in "./")
    }


def _is_test(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in TEST_MARKERS)


def _score(request: str, path: str, words: set[str], named: set[str]) -> float:
    """The rank score of one ``path`` against the request. Higher is shown first."""
    lowered = path.lower()
    parts = PurePosixPath(lowered)
    basename = parts.name
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    extension = basename.rsplit(".", 1)[1] if "." in basename else ""
    score = 0.0
    if lowered in named or basename in named:
        score += BASENAME_EXACT
    if basename and basename in request.lower():
        score += BASENAME_IN_REQUEST
    if stem and stem in words:
        score += STEM_IN_REQUEST
    if any(part in words for part in parts.parts[:-1]):
        score += DIR_IN_REQUEST
    if any(token in lowered for token in words if len(token) > 3):
        score += PATH_IN_REQUEST
    if extension and extension in words:
        score += EXTENSION_IN_REQUEST
    score -= DEPTH_PENALTY * max(0, len(parts.parts) - 1)
    if VENDOR_DIRS & set(parts.parts):
        score -= VENDOR_PENALTY
    if _is_test(lowered):
        score -= TEST_PENALTY
    return score


def rank(
    request: str,
    candidates: Iterable[Candidate],
    *,
    limit_bytes: int,
    max_files: int,
) -> list[str]:
    """The paths to show, best first.

    Every candidate with a positive score competes; zero and negative scores are dropped, so a
    request that names nothing specific yields no context rather than an arbitrary dump. The
    list stops at ``max_files`` or when the next file would exceed ``limit_bytes``; a file that
    does not fit is skipped and a smaller one further down may still be taken.
    """
    if max_files <= 0 or limit_bytes <= 0:
        return []
    words = _words(request)
    named = _named_paths(request)
    scored = [
        Scored(candidate.path, _score(request, candidate.path, words, named))
        for candidate in candidates
    ]
    # Best score first; ties on path so the order is stable across runs and platforms.
    scored = [entry for entry in scored if entry.score > 0]
    scored.sort(key=lambda entry: (-entry.score, entry.path))
    sizes = {candidate.path: candidate.size for candidate in candidates}
    chosen: list[str] = []
    total = 0
    for entry in scored:
        if len(chosen) >= max_files:
            break
        size = max(0, sizes.get(entry.path, 0))
        if total + size > limit_bytes:
            continue
        chosen.append(entry.path)
        total += size
    return chosen
