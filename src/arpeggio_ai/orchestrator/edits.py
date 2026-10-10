"""Experimental edit formats for patch mode (M0.8): search/replace blocks and whole files.

Only ``arpeggio eval run --edit-format`` reaches this module. EXE-08 (``udiff``) stays the
format of every other attempt. A ``search-replace`` reply holds blocks like::

    FILE: path/to/file.py
    <<<<<<< SEARCH
    lines copied exactly from the file
    =======
    the lines that replace them
    >>>>>>> REPLACE

and a ``whole-file`` reply holds, at most once per path::

    FILE: path/to/file.py
    <<<<<<< CONTENT
    the complete new file
    >>>>>>> END

``NEW FILE: path`` creates a file (with an empty SEARCH for search/replace). Files cannot
be deleted or renamed. Text outside blocks is ignored, and so is one code-fence line right
after the header. Inside a block only the format's own marker lines count, so a
``=======`` line is ordinary content of a whole file.

Whole files are written line by line with the file's class line break (``crlf`` or
``lf``), always ending with a line break. A ``mixed`` file is ``patch_mixed_line_endings``
because a rewrite would change its line breaks. Nothing checks that unchanged lines were
copied: the plan reports the largest share of an existing file's lines that went away.

Nothing is guessed. SEARCH is compared line by line with the file as it is in the worktree,
each line exactly (indentation, trailing spaces, tabs), without its line break. It must
match exactly once: no match is ``patch_search_not_found``, several are
``patch_search_ambiguous``. Every block is matched against the original file, never against
another block's result, and two blocks of one file that share a line are
``patch_edit_overlap``. When a SEARCH misses only because of trailing whitespace, the detail
says ``whitespace_near_miss``: a metric, never applied.

Line breaks: bytes outside the matched lines are kept as they are. Replacement lines take
the line break of the first matched line (the file's class, ``crlf`` or ``lf``, when that
line is the last one and has none), and the last replacement line takes the break of the
last matched line, so a file without a final newline keeps it that way. A new file gets
``new_file_ending`` like a diff's new file.

Checks run in this order before anything is written: size, path safety, ``[REDACTED:`` in
replacement lines, grounding against ``git ls-files`` of the base commit, symlink, binary
and UTF-8, then matching for every file. Only when all of that passes are the files
written, then ``commit_attempt`` checks the changed paths and line endings and commits.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from arpeggio_ai.orchestrator.patch import (
    MAX_PATCH_BYTES,
    EditFormat,
    Ending,
    PatchError,
    _path_problem,
    classify_line_endings,
    commit_attempt,
    new_file_ending,
)
from arpeggio_ai.safety.secret_scan import MARKER as REDACTION_MARKER
from arpeggio_ai.safety.worktree import git

log = logging.getLogger(__name__)

SEARCH, DIVIDER, REPLACE = "<<<<<<< SEARCH", "=======", ">>>>>>> REPLACE"
CONTENT, END = "<<<<<<< CONTENT", ">>>>>>> END"
# Marker lines per format, opening marker first and closing marker last.
MARKERS: dict[EditFormat, tuple[str, ...]] = {
    "search-replace": (SEARCH, DIVIDER, REPLACE),
    "whole-file": (CONTENT, END),
}
_HEADER = re.compile(r"^(NEW FILE|FILE):[ \t]*(.*?)\s*$")
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^`~]*$")
LINE_BREAKS: dict[Ending, str] = {"crlf": "\r\n", "lf": "\n", "mixed": "\n"}


@dataclass(frozen=True, slots=True)
class Edit:
    path: str
    new: bool  # declared with NEW FILE:
    search: tuple[str, ...]  # empty for a new file and for whole-file
    replace: tuple[str, ...]  # whole-file: the file's complete new content
    number: int  # 1-based position among the reply's blocks, for details
    whole: bool = False  # whole-file: replace is the whole file


@dataclass(frozen=True, slots=True)
class EditPlan:
    contents: dict[str, bytes]  # path -> bytes to write
    expected: dict[str, Ending]  # path -> class it must still have after writing
    shrink: float  # largest share of an existing file's lines removed (0.0 if none)


def _marker(line: str, markers: tuple[str, ...]) -> str | None:
    stripped = line.rstrip()
    return stripped if stripped in markers else None


def _block_lines(
    lines: list[str], index: int, path: str, edit_format: EditFormat
) -> tuple[list[list[str]], int]:
    """The parts of the block whose opening marker is at ``index - 1``: SEARCH and REPLACE,
    or the one CONTENT part. Returns them and the index after the closing marker."""
    markers = MARKERS[edit_format]
    closing = REPLACE if edit_format == "search-replace" else END
    parts: list[list[str]] = [[]]
    while True:
        if index >= len(lines):
            raise PatchError("patch_malformed", f"the block for {path} is not closed")
        line = lines[index]
        index += 1
        marker = _marker(line, markers)
        if marker is None:
            parts[-1].append(line)
        elif marker == DIVIDER and len(parts) == 1:
            parts.append([])
        elif marker == closing and len(parts) == len(markers) - 1:
            return parts, index
        else:
            raise PatchError(
                "patch_malformed", f"line {index}: unexpected {marker} in the block for {path}"
            )


def _check_block(edit: Edit) -> None:
    where = f"block {edit.number}: {'NEW FILE' if edit.new else 'FILE'}: {edit.path}"
    if edit.whole:
        return
    if edit.new and edit.search:
        raise PatchError("patch_malformed", f"{where} needs an empty SEARCH")
    if not edit.new and not edit.search:
        raise PatchError("patch_malformed", f"{where} has an empty SEARCH")
    if edit.search == edit.replace:
        raise PatchError("patch_malformed", f"{where} changes nothing")


def parse_edits(reply: str, edit_format: EditFormat = "search-replace") -> list[Edit]:
    """The blocks of ``reply`` in ``edit_format``, checked for structure only."""
    if edit_format not in MARKERS:
        raise ValueError(f"no edit blocks in the {edit_format} format")
    markers = MARKERS[edit_format]
    lines = [line.removesuffix("\r") for line in reply.split("\n")]
    edits: list[Edit] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        header = _HEADER.match(line)
        if header is None:
            marker = _marker(line, markers)
            if marker is not None:
                raise PatchError("patch_malformed", f"line {index}: {marker} outside a FILE: block")
            continue
        new, path = header.group(1) == "NEW FILE", header.group(2)
        if not path:
            raise PatchError("patch_malformed", f"line {index}: {header.group(1)}: without a path")
        if index < len(lines) and _FENCE_LINE.match(lines[index]):
            index += 1
        if index >= len(lines) or _marker(lines[index], markers) != markers[0]:
            raise PatchError(
                "patch_malformed",
                f"line {index}: {header.group(1)}: {path} is not followed by {markers[0]}",
            )
        parts, index = _block_lines(lines, index + 1, path, edit_format)
        whole = edit_format == "whole-file"
        search = () if whole else tuple(parts[0])
        edit = Edit(path, new, search, tuple(parts[-1]), len(edits) + 1, whole)
        _check_block(edit)
        edits.append(edit)
    if not edits:
        raise PatchError("patch_missing", "the reply has no FILE: block")
    paths = [edit.path for edit in edits]
    for path in sorted(set(paths)):
        mine = [edit for edit in edits if edit.path == path]
        if len(mine) > 1 and (edit_format == "whole-file" or any(e.new for e in mine)):
            what = (
                "written more than once"
                if edit_format == "whole-file"
                else ("declared as a new file and edited again")
            )
            raise PatchError("patch_ambiguous", f"{path} is {what}")
    return edits


def redacted_replacements(edits: list[Edit]) -> list[str]:
    """``path:block N line M`` (whole-file: ``path:line M``, a line of the new file) for every
    replacement line holding a ``[REDACTED:`` marker."""
    return [
        f"{edit.path}:line {number}"
        if edit.whole
        else f"{edit.path}:block {edit.number} line {number}"
        for edit in edits
        for number, line in enumerate(edit.replace, start=1)
        if REDACTION_MARKER in line
    ]


def split_lines(text: str) -> list[tuple[str, str]]:
    """``(content, line break)`` per line; the last line's break is ``""`` without a final
    newline. Only ``\\n`` and ``\\r\\n`` end a line."""
    pieces = text.split("\n")
    lines = [
        (piece[:-1], "\r\n") if piece.endswith("\r") else (piece, "\n") for piece in pieces[:-1]
    ]
    if pieces[-1]:
        lines.append((pieces[-1], ""))
    return lines


def _occurrences(contents: list[str], search: tuple[str, ...]) -> list[int]:
    size = len(search)
    return [
        start
        for start in range(len(contents) - size + 1)
        if tuple(contents[start : start + size]) == search
    ]


def _match(path: str, contents: list[str], edit: Edit) -> int:
    found = _occurrences(contents, edit.search)
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise PatchError(
            "patch_search_ambiguous",
            f"{path}: SEARCH of block {edit.number} matches {len(found)} places",
        )
    stripped = tuple(line.rstrip() for line in edit.search)
    near = len(_occurrences([line.rstrip() for line in contents], stripped)) == 1
    note = " (whitespace_near_miss)" if near else ""
    raise PatchError(
        "patch_search_not_found", f"{path}: SEARCH of block {edit.number} matches nothing{note}"
    )


def edit_file(path: str, text: str, ending: Ending, edits: list[Edit]) -> str:
    """Apply ``edits`` (all for ``path``) to ``text``, each matched against ``text`` itself."""
    lines = split_lines(text)
    contents = [content for content, _ in lines]
    spans: list[tuple[int, int, Edit]] = []
    for edit in edits:
        start = _match(path, contents, edit)
        spans.append((start, start + len(edit.search), edit))
    spans.sort(key=lambda span: span[0])
    for (_, end, first), (start, _, second) in pairwise(spans):
        if start < end:
            raise PatchError(
                "patch_edit_overlap",
                f"{path}: blocks {first.number} and {second.number} change the same lines",
            )
    out: list[str] = []
    at = 0
    for start, end, edit in spans:
        out.extend(content + brk for content, brk in lines[at:start])
        brk = lines[start][1] or LINE_BREAKS[ending]
        last = lines[end - 1][1]
        for number, line in enumerate(edit.replace, start=1):
            out.append(line + (last if number == len(edit.replace) else brk))
        at = end
    out.extend(content + brk for content, brk in lines[at:])
    return "".join(out)


async def plan_edits(
    edits: list[Edit], worktree: Path, *, env: Mapping[str, str], home: Path
) -> EditPlan:
    """Validate ``edits`` against ``worktree`` and compute the new bytes. Writes nothing."""
    size = sum(len("\n".join(e.search + e.replace).encode("utf-8")) for e in edits)
    if size > MAX_PATCH_BYTES:
        raise PatchError(
            "patch_unsafe", f"the edits are {size} bytes, over the {MAX_PATCH_BYTES}-byte limit"
        )
    for path in sorted({edit.path for edit in edits}):
        problem = _path_problem(path, worktree)
        if problem is not None:
            raise PatchError("patch_unsafe", f"{path}: {problem}")
    placeholders = redacted_replacements(edits)
    if placeholders:
        # Places only: the lines themselves may sit next to real secrets.
        raise PatchError(
            "patch_writes_redacted_placeholder",
            f"replacement lines contain a {REDACTION_MARKER}...] placeholder at "
            + ", ".join(placeholders),
        )
    listed = await git(["ls-files", "-z"], cwd=worktree, env=env, home=home)
    if listed.exit_code != 0:
        raise PatchError("patch_does_not_apply", f"git ls-files failed: {listed.text().strip()}")
    tracked = set(listed.text().split("\0"))
    unknown = sorted({e.path for e in edits if not e.new and e.path not in tracked})
    if unknown:
        raise PatchError(
            "patch_unknown_path",
            f"edits paths not in the base commit and not declared as NEW FILE: {unknown}",
        )
    contents: dict[str, bytes] = {}
    expected: dict[str, Ending] = {}
    shrink = 0.0
    for path in dict.fromkeys(edit.path for edit in edits):
        mine = [edit for edit in edits if edit.path == path]
        target = worktree / path
        if mine[0].new:
            if path in tracked or target.exists() or target.is_symlink():
                raise PatchError("patch_malformed", f"NEW FILE: {path} already exists")
            ending = new_file_ending(worktree, path)
            brk = LINE_BREAKS[ending]
            contents[path] = "".join(line + brk for line in mine[0].replace).encode("utf-8")
            expected[path] = ending
            continue
        if target.is_symlink() or not target.is_file():
            raise PatchError("patch_unsafe", f"{path}: symlinks and non-files are not allowed")
        data = target.read_bytes()
        if b"\0" in data:
            raise PatchError("patch_unsafe", f"{path}: binary files are not allowed")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise PatchError("patch_unsafe", f"{path}: not UTF-8 text") from None
        if any(REDACTION_MARKER in line for edit in mine for line in edit.search):
            # Context was redacted before it reached the model, so this SEARCH cannot match.
            log.warning("patch.touches_redacted_lines", extra={"paths": [path]})
        ending = classify_line_endings(data)
        if mine[0].whole:
            if ending == "mixed":
                raise PatchError(
                    "patch_mixed_line_endings",
                    f"{path} mixes CRLF and LF line endings; a rewrite would change them",
                )
            updated = "".join(line + LINE_BREAKS[ending] for line in mine[0].replace)
        else:
            updated = edit_file(path, text, ending, mine)
        contents[path] = updated.encode("utf-8")
        expected[path] = ending
        before, after = len(split_lines(text)), len(split_lines(updated))
        if before:
            shrink = max(shrink, (before - after) / before)
    return EditPlan(contents, expected, shrink)


def write_plan(plan: EditPlan, worktree: Path) -> None:
    """Write every planned file. Paths were validated by ``plan_edits``. If a write fails,
    the files already written get their old bytes back (new ones are removed), so a failed
    write never leaves part of the edits behind."""
    written: list[tuple[Path, bytes | None]] = []
    for path, data in plan.contents.items():
        target = worktree / path
        try:
            before = target.read_bytes() if target.is_file() else None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written.append((target, before))
        except OSError as error:
            for done, old in reversed(written):
                if old is None:
                    done.unlink()
                else:
                    done.write_bytes(old)
            raise PatchError("patch_does_not_apply", f"{path}: write failed: {error}") from None


async def apply_edits(
    reply: str,
    worktree: Path,
    *,
    edit_format: EditFormat,
    attempt_id: str,
    env: Mapping[str, str],
    home: Path,
    on_planned: Callable[[EditPlan], None] | None = None,
) -> str:
    """Parse, validate, write and commit the edits in ``reply``. Returns the commit SHA.

    ``on_planned`` receives the plan after every check passed and before anything is written.
    """
    edits = parse_edits(reply, edit_format)
    plan = await plan_edits(edits, worktree, env=env, home=home)
    if on_planned is not None:
        on_planned(plan)
    write_plan(plan, worktree)
    sha = await commit_attempt(
        worktree,
        attempt_id=attempt_id,
        env=env,
        home=home,
        declared=plan.contents,
        expected=plan.expected,
        what="edits",
    )
    log.info("patch.edits_applied", extra={"files": len(plan.contents), "format": edit_format})
    return sha
