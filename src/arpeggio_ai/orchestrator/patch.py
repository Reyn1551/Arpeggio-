"""Single-shot patch mode (EXE-08, ADR-0008): extract, validate, apply and commit one diff.

The model answers with exactly one fenced ``diff`` (or ``patch``) block. Before anything
touches the worktree the diff is checked: at most 200 KB, every path relative, without
``..``, outside ``.git``, not C-quoted and resolving inside the worktree, no binary data,
no symlinks, and no added line holding a ``[REDACTED:`` placeholder copied from the
redacted context (``patch_writes_redacted_placeholder``). ``git apply --check`` and
``git apply`` then run with the scrubbed environment, the changed files must be exactly
paths the diff declared, and the result is committed on the attempt branch as
``Arpeggio <arpeggio@localhost>``.

Hunk counts are recounted from the hunk bodies (M0.7) when the reply ended with
``finish_reason`` ``stop``: models often get the ``@@`` counts wrong while the lines are
right. A truncated reply is never recounted, so a cut-off diff still fails as
``output_truncated``. A line inside a hunk that is not a diff line is ``patch_malformed``,
and so is a non-rename section whose ``diff --git``, ``---`` and ``+++`` headers name two
different files or a ``diff --git`` line that is not ``a/<path> b/<path>``.
The prompt lists the exact context paths as the only files to modify, and a diff that
modifies, deletes or renames a path missing from the worktree's base commit (``git
ls-files`` in the attempt worktree, compared exactly) is ``patch_unknown_path``.

Line endings are normalized per file before ``git apply`` (EXE-08). Each modified or
deleted file is classified from the worktree: ``crlf`` when at least 95% of its line breaks
are CRLF, ``lf`` when at least 95% are a bare LF, else ``mixed``. A new file is ``crlf``
only if the root ``.gitattributes`` gives its path ``eol=crlf`` (patterns ``*``, ``*.ext``
or an exact path; the last match wins), else ``lf``. Hunk lines of a ``crlf`` file get
CRLF, those of an ``lf`` file lose any ``\\r``, and a ``mixed`` file's hunks stay as the
model wrote them. If such a diff does not apply the reason is ``patch_mixed_line_endings``.
After applying, every touched ``crlf`` or ``lf`` file must still have that class
(``patch_line_endings_changed`` otherwise; files without a line break always match).

Failures raise ``PatchError`` with one of the reasons stored in
``attempts.failure_reason``. ``truncation_reason`` turns ``patch_missing`` or
``patch_does_not_apply`` into ``output_truncated`` when the reply stopped at the output
limit; the other reasons stay as they are.
"""

import fnmatch
import logging
import re
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.safety.secret_scan import MARKER as REDACTION_MARKER
from arpeggio_ai.safety.worktree import git
from arpeggio_ai.verify.criteria import relative_path_problem

log = logging.getLogger(__name__)

FailureReason = Literal[
    "patch_missing",
    "patch_ambiguous",
    "patch_unsafe",
    "patch_does_not_apply",
    "patch_mixed_line_endings",
    "patch_line_endings_changed",
    "patch_writes_redacted_placeholder",
    "patch_malformed",
    "patch_unknown_path",
    "output_truncated",
]
# Patch failures a reply cut off at the output limit explains (M0.6.1).
TRUNCATION_EXPLAINS: frozenset[FailureReason] = frozenset({"patch_missing", "patch_does_not_apply"})
Ending = Literal["crlf", "lf", "mixed"]
MAX_PATCH_BYTES = 200 * 1024
LINE_ENDING_SHARE = 0.95
CONTEXT_CAP_BYTES = 100 * 1024
AUTHOR_NAME = "Arpeggio"
AUTHOR_EMAIL = "arpeggio@localhost"
# Default context exclusions from docs/07-SECURITY-AND-PRIVACY.md (SAF-02).
SECRET_PATTERNS = (".env*", "*.pem", "*.key", "id_*", "*credentials*", "secrets.*")

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`~]*)")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_HUNK_NEW_START = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")
_HUNK_FULL = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_SYMLINK_MODE = re.compile(r"^(new file|deleted file|old|new) mode 120000\s*$")


class PatchError(ArpeggioError):
    def __init__(self, reason: FailureReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: FailureReason = reason
        self.detail = detail


def truncation_reason(error: PatchError, truncated: bool) -> tuple[FailureReason, str]:
    """(reason, detail) to store. A truncated reply whose patch is missing or does not apply
    is ``output_truncated``, and its detail keeps the original reason."""
    if truncated and error.reason in TRUNCATION_EXPLAINS:
        return "output_truncated", f"{error.reason}: {error.detail}"
    return error.reason, error.detail


def system_prompt() -> str:
    """The packaged instruction that asks for exactly one unified diff."""
    resource = files("arpeggio_ai.orchestrator").joinpath("prompts", "patch_system.txt")
    return resource.read_text(encoding="utf-8")


def extract_patch(text: str) -> str:
    """The content of the one fenced ``diff``/``patch`` block in ``text``."""
    blocks: list[tuple[str, list[str]]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        index += 1
        if opening is None:
            continue
        fence, info = opening.group(1), opening.group(2).lower()
        closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$")
        body: list[str] = []
        while index < len(lines) and not closing.match(lines[index]):
            body.append(lines[index])
            index += 1
        index += 1  # skip the closing fence (or run past the end of an unclosed block)
        blocks.append((info, body))
    diffs = [body for info, body in blocks if info in ("diff", "patch")]
    if not diffs:
        raise PatchError("patch_missing", "the reply has no ```diff block")
    if len(diffs) > 1:
        raise PatchError("patch_ambiguous", f"the reply has {len(diffs)} diff blocks; expected one")
    return "\n".join(diffs[0]) + "\n"


def _path_problem(path: str, worktree: Path) -> str | None:
    if path.startswith('"'):
        return "quoted paths are not supported"
    problem = relative_path_problem(path)
    if problem is not None:
        return problem
    if any(part.lower() == ".git" for part in path.split("/")):
        return "must not touch .git"
    root = worktree.resolve()
    if not (worktree / path).resolve().is_relative_to(root):
        return "resolves outside the worktree"
    return None


def _header_path(raw: str) -> str | None:
    """Path from a ``---``/``+++`` line, with the a/ or b/ prefix removed like ``git apply -p1``."""
    raw = raw.split("\t", 1)[0].rstrip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"'):
        return raw
    if raw.startswith("/") or _DRIVE.match(raw) or "\\" in raw:
        return raw  # rejected by _path_problem as absolute
    return raw.split("/", 1)[1] if "/" in raw else raw


def validate_patch(diff: str, worktree: Path) -> list[str]:
    """Check the diff without applying it. Returns the paths it declares, sorted."""
    size = len(diff.encode("utf-8"))
    if size > MAX_PATCH_BYTES:
        raise PatchError(
            "patch_unsafe", f"the diff is {size} bytes, over the {MAX_PATCH_BYTES}-byte limit"
        )
    declared: set[str] = set()
    old_left = new_left = 0
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            marker = line[:1]
            if marker == " " or line == "":
                old_left, new_left = old_left - 1, new_left - 1
                continue
            if marker == "-":
                old_left -= 1
                continue
            if marker == "+":
                new_left -= 1
                continue
            if marker == "\\":
                continue
            old_left = new_left = 0  # malformed hunk: let git apply report it
        hunk = _HUNK.match(line)
        if hunk:
            old_left = int(hunk.group(1) or 1)
            new_left = int(hunk.group(2) or 1)
            continue
        if line.startswith("GIT binary patch") or (
            line.startswith("Binary files ") and line.rstrip().endswith(" differ")
        ):
            raise PatchError("patch_unsafe", "binary patches are not allowed")
        if _SYMLINK_MODE.match(line) or (
            line.startswith("index ") and line.rstrip().endswith(" 120000")
        ):
            raise PatchError("patch_unsafe", "symlinks are not allowed")
        if line.startswith("diff --git "):
            if '"' in line:
                raise PatchError("patch_unsafe", f"quoted paths are not supported: {line[11:]}")
            match = _DIFF_GIT.match(line)
            if match:
                declared.update(match.groups())
            continue
        for prefix in ("--- ", "+++ "):
            if line.startswith(prefix):
                path = _header_path(line[len(prefix) :])
                if path is not None:
                    declared.add(path)
        for prefix in ("rename from ", "rename to ", "copy from ", "copy to "):
            if line.startswith(prefix):
                declared.add(line[len(prefix) :].rstrip())
    if not declared:
        raise PatchError("patch_does_not_apply", "the diff names no files")
    for path in sorted(declared):
        problem = _path_problem(path, worktree)
        if problem is not None:
            raise PatchError("patch_unsafe", f"{path}: {problem}")
    problem = header_problem(parse_diff(diff))
    if problem is not None:
        raise PatchError("patch_malformed", problem)
    placeholders = redacted_added_lines(parse_diff(diff))
    if placeholders:
        # Paths and line numbers only: the lines themselves may sit next to real secrets.
        where = ", ".join(f"{path}:{line}" for path, line in placeholders)
        raise PatchError(
            "patch_writes_redacted_placeholder",
            f"added lines contain a {REDACTION_MARKER}...] placeholder at {where}",
        )
    return sorted(declared)


def recount_hunks(diff: str) -> tuple[str, int]:
    """Rewrite each hunk's ``@@`` counts from its body. Returns the diff and the number of
    hunks whose counts changed; a hunk whose counts are right keeps its header byte for byte.

    A hunk body runs until the next ``@@``, ``diff --git`` or ``---``/``+++`` pair (a pair
    still inside the declared counts is a removed and an added line, as in git) or the end.
    Context and blank lines count on both sides, ``-`` on the old side, ``+`` on the new one.
    Blank lines at the end of a hunk are kept but not counted. Any other line inside a hunk
    is ``patch_malformed``, unless the counted lines already match the header, where git
    ends the hunk too. No line is ever added or removed.
    """
    lines = diff.split("\n")
    out = list(lines)
    changed = 0
    header: re.Match[str] | None = None
    at = old = new = blanks = 0

    def close() -> None:
        nonlocal header, changed
        if header is None:
            return
        old_start, old_count, new_start, new_count, rest = header.groups()
        if (old, new) != (int(old_count or 1), int(new_count or 1)):
            ending = "\r" if lines[at].endswith("\r") else ""
            out[at] = f"@@ -{old_start},{old} +{new_start},{new} @@{rest}{ending}"
            changed += 1
        header = None

    for number, raw in enumerate(lines):
        line = raw.removesuffix("\r")
        if header is not None:
            declared_old, declared_new = int(header.group(2) or 1), int(header.group(4) or 1)
            inside = old < declared_old and new < declared_new
            pair = (
                line.startswith("--- ")
                and number + 1 < len(lines)
                and lines[number + 1].startswith("+++ ")
            )
            if line.startswith(("@@ ", "diff --git ")) or (pair and not inside):
                close()
            elif line == "":
                blanks += 1
                continue
            elif line[:1] in (" ", "-", "+", "\\"):
                old += blanks + (line[:1] in (" ", "-"))
                new += blanks + (line[:1] in (" ", "+"))
                blanks = 0
                continue
            elif (old, new) == (declared_old, declared_new):
                close()
            else:
                raise PatchError("patch_malformed", f"diff line {number + 1} is not a hunk line")
        match = _HUNK_FULL.match(line)
        if match:
            header, at, old, new, blanks = match, number, 0, 0, 0
    close()
    return "\n".join(out), changed


LineKind = Literal["header", "hunk", "context", "removed", "added", "no_newline"]


@dataclass(slots=True)
class FileSection:
    """One file's part of a diff. ``old`` is None for a new file, ``new`` for a deletion."""

    old: str | None = None
    new: str | None = None
    has_old_header: bool = False
    has_hunk: bool = False


@dataclass(frozen=True, slots=True)
class ParsedDiff:
    lines: list[str]  # the diff split on "\n", each line without it
    kinds: list[tuple[LineKind, int]]  # per line: its kind and its section index (-1: none)
    sections: list[FileSection]


def parse_diff(diff: str) -> ParsedDiff:
    """Split a diff into file sections and classify every line (hunk counts as in git)."""
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    kinds: list[tuple[LineKind, int]] = []
    sections: list[FileSection] = []
    old_left = new_left = 0
    for raw in lines:
        line = raw.rstrip("\r")
        current = len(sections) - 1
        if old_left > 0 or new_left > 0:
            marker = line[:1]
            kind: LineKind | None = None
            if marker == " " or line == "":
                kind, old_left, new_left = "context", old_left - 1, new_left - 1
            elif marker == "-":
                kind, old_left = "removed", old_left - 1
            elif marker == "+":
                kind, new_left = "added", new_left - 1
            elif marker == "\\":
                kind = "no_newline"
            else:
                old_left = new_left = 0  # malformed hunk: let git apply report it
            if kind is not None:
                kinds.append((kind, current))
                continue
        if line.startswith("\\") and kinds and kinds[-1][0] in ("context", "removed", "added"):
            kinds.append(("no_newline", current))  # follows the last line of a hunk
            continue
        hunk = _HUNK.match(line)
        if hunk and current >= 0:
            old_left, new_left = int(hunk.group(1) or 1), int(hunk.group(2) or 1)
            sections[current].has_hunk = True
            kinds.append(("hunk", current))
            continue
        match = _DIFF_GIT.match(line)
        if line.startswith("diff --git "):
            sections.append(FileSection(*(match.groups() if match else (None, None))))
        elif line.startswith("--- "):
            section = sections[current] if current >= 0 else None
            if section is None or section.has_hunk or section.has_old_header:
                section = FileSection()
                sections.append(section)
            section.has_old_header = True
            section.old = _header_path(line[4:])
        elif line.startswith("+++ ") and current >= 0:
            sections[current].new = _header_path(line[4:])
        elif line.startswith("new file mode") and current >= 0:
            sections[current].old = None
        elif line.startswith("deleted file mode") and current >= 0:
            sections[current].new = None
        elif line.startswith("rename from ") and current >= 0:
            sections[current].old = line[len("rename from ") :]
        elif line.startswith("rename to ") and current >= 0:
            sections[current].new = line[len("rename to ") :]
        kinds.append(("header", len(sections) - 1))
    return ParsedDiff(lines, kinds, sections)


def redacted_added_lines(parsed: ParsedDiff) -> list[tuple[str, int]]:
    """``(path, line in the new file)`` for every added line holding a ``[REDACTED:`` marker."""
    found: list[tuple[str, int]] = []
    line_number = 0
    for line, (kind, index) in zip(parsed.lines, parsed.kinds, strict=True):
        if kind == "hunk":
            start = _HUNK_NEW_START.match(line)
            line_number = int(start.group(1)) if start else 0
        elif kind == "context":
            line_number += 1
        elif kind == "added":
            if REDACTION_MARKER in line and index >= 0:
                section = parsed.sections[index]
                found.append((section.new or section.old or "?", line_number))
            line_number += 1
    return found


def header_problem(parsed: ParsedDiff) -> str | None:
    """Why the headers of a non-rename, non-copy file section name two different files.

    ``diff --git`` must read ``a/<path> b/<path>``, and those paths and the ``---``/``+++``
    paths (``/dev/null`` aside) must all be the same file (M0.7).
    """
    names: dict[int, list[str]] = {}
    moved: set[int] = set()
    for line, (kind, index) in zip(parsed.lines, parsed.kinds, strict=True):
        line = line.rstrip("\r")
        if kind != "header" or index < 0:
            continue
        if line.startswith("diff --git "):
            match = _DIFF_GIT.match(line)
            if match is None:
                left, _, right = line[len("diff --git ") :].partition(" ")
                return f"diff --git needs a/<path> b/<path>, got {left} and {right}"
            names.setdefault(index, []).extend(match.groups())
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            moved.add(index)
        elif line.startswith(("--- ", "+++ ")):
            path = _header_path(line[4:])
            if path is not None:
                names.setdefault(index, []).append(path)
    for index, paths in sorted(names.items()):
        if index in moved:
            continue
        different = sorted(set(paths))
        if len(different) > 1:
            return f"the a/ and b/ paths name different files: {different[0]} and {different[1]}"
    return None


def unknown_paths(parsed: ParsedDiff, tracked: Collection[str]) -> list[str]:
    """Paths the diff modifies, deletes or renames from that are not in ``tracked`` (the
    files of the base commit). A new file (``/dev/null`` or ``new file mode``) is never
    unknown. Compared exactly, so a wrong case is unknown on Windows too."""
    return sorted({s.old for s in parsed.sections if s.old is not None and s.old not in tracked})


def redacted_hunk_paths(parsed: ParsedDiff) -> list[str]:
    """Files whose hunk context or removed lines hold a ``[REDACTED:`` marker."""
    paths: set[str] = set()
    for line, (kind, index) in zip(parsed.lines, parsed.kinds, strict=True):
        if kind in ("context", "removed") and REDACTION_MARKER in line and index >= 0:
            section = parsed.sections[index]
            paths.add(section.old or section.new or "?")
    return sorted(paths)


# Line endings (EXE-08)


def classify_line_endings(data: bytes) -> Ending:
    """``crlf`` or ``lf`` when at least 95% of line breaks are that kind, else ``mixed``."""
    breaks = data.count(b"\n")
    if breaks == 0:
        return "lf"
    crlf = data.count(b"\r\n")
    if crlf / breaks >= LINE_ENDING_SHARE:
        return "crlf"
    if (breaks - crlf) / breaks >= LINE_ENDING_SHARE:
        return "lf"
    return "mixed"


def _attribute_matches(pattern: str, path: str) -> bool:
    """``*``, ``*.ext`` (any directory) or an exact path; other globs are not supported."""
    if pattern == "*":
        return True
    if pattern.startswith("*.") and not any(c in pattern[2:] for c in "*?[/"):
        return path.rsplit("/", 1)[-1].endswith(pattern[1:])
    if any(c in pattern for c in "*?["):
        return False
    return pattern.removeprefix("/") == path


def new_file_ending(worktree: Path, path: str) -> Ending:
    """``crlf`` if the root ``.gitattributes`` assigns ``eol=crlf`` to ``path``, else ``lf``."""
    attributes = worktree / ".gitattributes"
    if not attributes.is_file():
        return "lf"
    ending: Ending = "lf"
    for raw in attributes.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = raw.split()
        if not fields or fields[0].startswith("#") or not _attribute_matches(fields[0], path):
            continue
        for attribute in fields[1:]:
            if attribute == "eol=crlf":
                ending = "crlf"
            elif attribute in ("eol=lf", "-eol", "!eol", "-text", "binary"):
                ending = "lf"
    return ending


@dataclass(frozen=True, slots=True)
class LineEndingPlan:
    diff: str  # the diff to apply
    expected: dict[str, Ending]  # path after applying -> class it must still have
    mixed: list[str]  # files left untouched because their line endings are mixed


def normalize_line_endings(diff: str, worktree: Path) -> LineEndingPlan:
    """Give every hunk line the line ending of its file. Paths must be validated first."""
    parsed = parse_diff(diff)
    endings: list[Ending] = []
    expected: dict[str, Ending] = {}
    for section in parsed.sections:
        if section.old is None:
            ending = new_file_ending(worktree, section.new) if section.new else "lf"
        else:
            source = worktree / section.old
            ending = classify_line_endings(source.read_bytes()) if source.is_file() else "lf"
        endings.append(ending)
        if section.new is not None:
            expected[section.new] = ending
    out: list[str] = []
    for number, (line, (kind, index)) in enumerate(zip(parsed.lines, parsed.kinds, strict=True)):
        body = line.removesuffix("\r")
        if kind not in ("context", "removed", "added") or index < 0:
            out.append(body + "\n")
            continue
        ending = endings[index]
        if ending == "mixed":
            out.append(line + "\n")
            continue
        if ending == "crlf":
            if body == "":
                body = " "  # a blank context line; "\r" alone would not parse
            last = number + 1 < len(parsed.kinds) and parsed.kinds[number + 1][0] == "no_newline"
            out.append(body + ("\n" if last else "\r\n"))
        else:
            out.append(body + "\n")
    mixed = sorted(
        section.old or section.new or "?"
        for section, ending in zip(parsed.sections, endings, strict=True)
        if ending == "mixed"
    )
    return LineEndingPlan("".join(out), expected, mixed)


def _endings_problem(worktree: Path, expected: Mapping[str, Ending]) -> str | None:
    for path, ending in sorted(expected.items()):
        target = worktree / path
        if ending == "mixed" or not target.is_file():
            continue
        data = target.read_bytes()
        if b"\n" in data and classify_line_endings(data) != ending:
            return f"{path} was {ending} and is {classify_line_endings(data)} after applying"
    return None


def _changed_paths(porcelain_z: str) -> set[str]:
    """Paths from ``git status --porcelain -z`` (both sides of a rename)."""
    entries = porcelain_z.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.add(path)
        if "R" in status or "C" in status:
            paths.add(entries[index])
            index += 1
    return paths


async def apply_patch(
    diff: str,
    worktree: Path,
    *,
    attempt_id: str,
    env: Mapping[str, str],
    home: Path,
    on_normalized: Callable[[LineEndingPlan], None] | None = None,
    recount: bool = False,
) -> str:
    """Validate, normalize, apply and commit ``diff`` in ``worktree``. Returns the commit SHA.

    ``recount`` rewrites the hunk counts from their bodies (``recount_hunks``) after the raw
    diff passed validation; the result is validated again. Only a complete reply
    (``finish_reason`` ``stop``) is recounted. ``on_normalized`` receives the diff that will
    be applied, before ``git apply`` runs, so it can be stored whether or not it applies.
    """
    declared = set(validate_patch(diff, worktree))
    if recount:
        diff, recounted = recount_hunks(diff)
        if recounted:
            log.info("patch.hunks_recounted", extra={"hunks": recounted})
            declared = set(validate_patch(diff, worktree))
    tracked = await git(["ls-files", "-z"], cwd=worktree, env=env, home=home)
    if tracked.exit_code != 0:
        raise PatchError("patch_does_not_apply", f"git ls-files failed: {tracked.text().strip()}")
    unknown = unknown_paths(parse_diff(diff), set(tracked.text().split("\0")))
    if unknown:
        raise PatchError(
            "patch_unknown_path",
            f"modifies paths not in the base commit and not created by the diff: {unknown}",
        )
    redacted = redacted_hunk_paths(parse_diff(diff))
    if redacted:
        # Context was redacted before it reached the model, so these hunks may not match.
        log.warning("patch.touches_redacted_lines", extra={"paths": redacted})
    plan = normalize_line_endings(diff, worktree)
    if on_normalized is not None:
        on_normalized(plan)
    with tempfile.TemporaryDirectory(prefix="arpeggio-patch-") as scratch:
        patch_file = Path(scratch) / "attempt.diff"
        patch_file.write_bytes(plan.diff.encode("utf-8"))
        for check in (["--check"], []):
            result = await git(
                ["apply", *check, "--whitespace=nowarn", str(patch_file)],
                cwd=worktree,
                env=env,
                home=home,
            )
            if result.exit_code != 0 or result.timed_out:
                detail = result.text().strip() or "git apply failed"
                if plan.mixed:
                    detail = f"{', '.join(plan.mixed)} mixed CRLF and LF line endings: {detail}"
                    raise PatchError("patch_mixed_line_endings", detail)
                raise PatchError("patch_does_not_apply", detail)

    status = await git(
        ["status", "--porcelain", "-z", "--untracked-files=all"], cwd=worktree, env=env, home=home
    )
    changed = _changed_paths(status.text())
    if not changed:
        raise PatchError("patch_does_not_apply", "the diff applied but changed nothing")
    undeclared = sorted(changed - declared)
    if undeclared:
        raise PatchError("patch_unsafe", f"changed paths the diff did not declare: {undeclared}")
    problem = _endings_problem(worktree, plan.expected)
    if problem is not None:
        raise PatchError("patch_line_endings_changed", problem)

    commit_env = {
        **env,
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
    }
    for args in (
        ["add", "-A"],
        ["commit", "--no-verify", "-q", "-m", f"arpeggio: attempt {attempt_id}"],
    ):
        result = await git(args, cwd=worktree, env=commit_env, home=home)
        if result.exit_code != 0:
            raise PatchError(
                "patch_does_not_apply", f"git {args[0]} failed: {result.text().strip()}"
            )
    head = await git(["rev-parse", "HEAD"], cwd=worktree, env=env, home=home)
    sha = head.text().strip()
    log.info("patch.applied", extra={"files": len(changed), "commit": sha})
    return sha


def read_context(
    worktree: Path,
    paths: Sequence[str],
    cap_bytes: int = CONTEXT_CAP_BYTES,
    *,
    hidden: Collection[str] = (),
) -> list[tuple[str, str]]:
    """Read context files from the worktree for the prompt, skipping unsafe or secret ones.

    A path in ``hidden`` (the hidden tests of an attached eval task) is refused before
    anything is read, compared case-insensitively so Windows spellings cannot slip past.
    """
    blocked = {path.casefold() for path in hidden}
    chosen: list[tuple[str, str]] = []
    total = 0
    for path in paths:
        if path.casefold() in blocked:
            log.warning("patch.context_refused_hidden_test", extra={"path": path})
            continue
        reason = _path_problem(path, worktree)
        name = path.rsplit("/", 1)[-1].lower()
        target = worktree / path
        if reason is None and any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS):
            reason = "matches a secret file pattern"
        if reason is None and not target.is_file():
            reason = "not a file in the worktree"
        data = b""
        if reason is None:
            data = target.read_bytes()
            if total + len(data) > cap_bytes:
                reason = f"over the {cap_bytes}-byte context cap"
        if reason is None:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                reason = "not UTF-8 text"
        if reason is not None:
            log.warning("patch.context_skipped", extra={"path": path, "reason": reason})
            continue
        chosen.append((path, text))
        total += len(data)
    return chosen


def build_prompt(
    request: str,
    checks: Sequence[str],
    context: Sequence[tuple[str, str]],
    feedback: str | None = None,
) -> str:
    """The user message: the task, the checks that judge it, the context files and, for an
    escalated attempt, the previous attempt's failure report (VER-03). The exact context
    paths are listed as the only files the diff may modify (M0.7)."""
    parts = [f"Task:\n{request.strip()}", "Checks that must pass after your diff is applied:"]
    parts.append("\n".join(f"{n}. {check}" for n, check in enumerate(checks, start=1)))
    if context:
        listing = "\n".join(f"- {path}" for path, _ in context)
        parts.append(
            f"Files you may modify, with these exact paths:\n{listing}\n"
            "Modify only these files. Create a new file only when the task needs one."
        )
        parts.append("Files from the repository (paths relative to the root):")
        for path, text in context:
            parts.append(f"File `{path}`:\n````\n{text}\n````")
    if feedback:
        parts.append(f"Previous attempt:\n{feedback.strip()}")
    return "\n\n".join(parts) + "\n"
