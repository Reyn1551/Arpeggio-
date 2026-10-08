"""Choosing files: a ``**``-aware glob matcher and the walk behind ``arpeggio secrets scan``.

Globs match POSIX paths relative to a root: ``*`` and ``?`` stay inside one segment, ``**``
spans any number of segments (``tests/**`` matches everything under ``tests``, ``**/x.py``
matches ``x.py`` at any depth, the root included).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from arpeggio_ai.safety.secret_scan import SecretScanner
from arpeggio_ai.safety.worktree import git

MAX_SCAN_BYTES = 5 * 1024 * 1024
BINARY_PROBE_BYTES = 8192


@cache
def _compile(pattern: str) -> re.Pattern[str]:
    out = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out))


def glob_match(pattern: str, path: str) -> bool:
    return _compile(pattern).fullmatch(path) is not None


def matches_any(patterns: Iterable[str], path: str) -> bool:
    return any(glob_match(pattern, path) for pattern in patterns)


@dataclass(slots=True)
class FileSet:
    root: Path
    files: list[str] = field(default_factory=list)  # POSIX, relative to root
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


async def _git_files(root: Path, home: Path, env: Mapping[str, str]) -> list[str] | None:
    """Tracked and untracked-but-not-ignored files under ``root``, or None outside git."""
    inside = await git(["rev-parse", "--is-inside-work-tree"], cwd=root, env=env, home=home)
    if inside.exit_code != 0 or inside.text().strip() != "true":
        return None
    listing = await git(
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        env=env,
        home=home,
    )
    if listing.exit_code != 0 or listing.output_limit_exceeded:
        return None
    return sorted({name for name in listing.text().split("\0") if name})


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_PROBE_BYTES]


async def collect(
    root: Path,
    *,
    home: Path,
    env: Mapping[str, str],
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> FileSet:
    """Files under ``root`` to scan: ``git ls-files`` inside a repository (so ``.gitignore``
    applies), else every file outside ``.git``. Applies the include and exclude globs, skips
    files over 5 MB; binary files are skipped later by ``read_text``."""
    root = root.resolve()
    result = FileSet(root)
    names = await _git_files(root, home, env)
    if names is None:
        names = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts
        )
    for name in names:
        if include and not matches_any(include, name):
            continue
        if exclude and matches_any(exclude, name):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            result.skip("not_a_file")
        elif path.stat().st_size > MAX_SCAN_BYTES:
            result.skip("too_large")
        else:
            result.files.append(name)
    return result


def read_text(path: Path) -> str | None:
    """The file as text, or None when it is binary."""
    data = path.read_bytes()
    if is_binary(data):
        return None
    return data.decode("utf-8", errors="replace")


@dataclass(slots=True)
class ScanReport:
    """Where findings are, never what they are: no matched text, no hash of it (SAF-02)."""

    files_scanned: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    findings: list[tuple[str, int, str]] = field(default_factory=list)  # file, line, type

    def totals(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, _, kind in self.findings:
            counts[kind] = counts.get(kind, 0) + 1
        return dict(sorted(counts.items()))

    def top_files(self, limit: int = 20) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for name, _, _ in self.findings:
            counts[name] = counts.get(name, 0) + 1
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]

    def to_dict(self) -> dict[str, object]:
        return {
            "files_scanned": self.files_scanned,
            "skipped": dict(sorted(self.skipped.items())),
            "totals": self.totals(),
            "top_files": [{"file": name, "count": n} for name, n in self.top_files()],
            "findings": [
                {"file": name, "line": line, "type": kind} for name, line, kind in self.findings
            ],
        }


def scan_files(files: FileSet, scanner: SecretScanner) -> ScanReport:
    """Run ``scanner`` over every file and keep only file, line and type of each finding."""
    report = ScanReport(skipped=dict(files.skipped))
    for name in files.files:
        try:
            text = read_text(files.root / name)
        except OSError:
            report.skipped["unreadable"] = report.skipped.get("unreadable", 0) + 1
            continue
        if text is None:
            report.skipped["binary"] = report.skipped.get("binary", 0) + 1
            continue
        report.files_scanned += 1
        for finding in scanner.scan(text, name).findings:
            report.findings.append((name, finding.line, finding.type))
    return report
