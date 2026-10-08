"""Single-shot patch mode (EXE-08, ADR-0008): extract, validate, apply and commit one diff.

The model answers with exactly one fenced ``diff`` (or ``patch``) block. Before anything
touches the worktree the diff is checked: at most 200 KB, every path relative, without
``..``, outside ``.git``, not C-quoted and resolving inside the worktree, no binary data,
no symlinks. ``git apply --check`` and ``git apply`` then run with the scrubbed
environment, the changed files must be exactly paths the diff declared, and the result is
committed on the attempt branch as ``Arpeggio <arpeggio@localhost>``.

Failures raise ``PatchError`` with one of the reasons stored in
``attempts.failure_reason``.
"""

import fnmatch
import logging
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from arpeggio_ai.core.errors import ArpeggioError
from arpeggio_ai.safety.secret_scan import MARKER as REDACTION_MARKER
from arpeggio_ai.safety.worktree import git
from arpeggio_ai.verify.criteria import relative_path_problem

log = logging.getLogger(__name__)

FailureReason = Literal["patch_missing", "patch_ambiguous", "patch_unsafe", "patch_does_not_apply"]
MAX_PATCH_BYTES = 200 * 1024
CONTEXT_CAP_BYTES = 100 * 1024
AUTHOR_NAME = "Arpeggio"
AUTHOR_EMAIL = "arpeggio@localhost"
# Default context exclusions from docs/07-SECURITY-AND-PRIVACY.md (SAF-02).
SECRET_PATTERNS = (".env*", "*.pem", "*.key", "id_*", "*credentials*", "secrets.*")

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`~]*)")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_DIFF_GIT = re.compile(r"^diff --git a/(.+) b/(.+)$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_SYMLINK_MODE = re.compile(r"^(new file|deleted file|old|new) mode 120000\s*$")


class PatchError(ArpeggioError):
    def __init__(self, reason: FailureReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: FailureReason = reason
        self.detail = detail


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
    return sorted(declared)


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


def redacted_hunk_paths(parsed: ParsedDiff) -> list[str]:
    """Files whose hunk context or removed lines hold a ``[REDACTED:`` marker."""
    paths: set[str] = set()
    for line, (kind, index) in zip(parsed.lines, parsed.kinds, strict=True):
        if kind in ("context", "removed") and REDACTION_MARKER in line and index >= 0:
            section = parsed.sections[index]
            paths.add(section.old or section.new or "?")
    return sorted(paths)


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
    diff: str, worktree: Path, *, attempt_id: str, env: Mapping[str, str], home: Path
) -> str:
    """Validate, apply and commit ``diff`` in ``worktree``. Returns the new commit SHA."""
    declared = set(validate_patch(diff, worktree))
    redacted = redacted_hunk_paths(parse_diff(diff))
    if redacted:
        # Context was redacted before it reached the model, so these hunks may not match.
        log.warning("patch.touches_redacted_lines", extra={"paths": redacted})
    with tempfile.TemporaryDirectory(prefix="arpeggio-patch-") as scratch:
        patch_file = Path(scratch) / "attempt.diff"
        patch_file.write_bytes(diff.encode("utf-8"))
        for check in (["--check"], []):
            result = await git(
                ["apply", *check, "--whitespace=nowarn", str(patch_file)],
                cwd=worktree,
                env=env,
                home=home,
            )
            if result.exit_code != 0 or result.timed_out:
                raise PatchError(
                    "patch_does_not_apply", result.text().strip() or "git apply failed"
                )

    status = await git(
        ["status", "--porcelain", "-z", "--untracked-files=all"], cwd=worktree, env=env, home=home
    )
    changed = _changed_paths(status.text())
    if not changed:
        raise PatchError("patch_does_not_apply", "the diff applied but changed nothing")
    undeclared = sorted(changed - declared)
    if undeclared:
        raise PatchError("patch_unsafe", f"changed paths the diff did not declare: {undeclared}")

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
    worktree: Path, paths: Sequence[str], cap_bytes: int = CONTEXT_CAP_BYTES
) -> list[tuple[str, str]]:
    """Read context files from the worktree for the prompt, skipping unsafe or secret ones."""
    chosen: list[tuple[str, str]] = []
    total = 0
    for path in paths:
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


def build_prompt(request: str, checks: Sequence[str], context: Sequence[tuple[str, str]]) -> str:
    """The user message: the task, the checks that judge it and the context files."""
    parts = [f"Task:\n{request.strip()}", "Checks that must pass after your diff is applied:"]
    parts.append("\n".join(f"- {check}" for check in checks))
    if context:
        parts.append("Files from the repository (paths relative to the root):")
        for path, text in context:
            parts.append(f"File `{path}`:\n````\n{text}\n````")
    return "\n\n".join(parts) + "\n"
