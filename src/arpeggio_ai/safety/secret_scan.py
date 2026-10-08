"""Find secrets in text and replace them with ``[REDACTED:<type>]`` (SAF-02, NFR-06).

Three chokepoints call this: outbound model requests (``adapters.api``), artifact writes
(``store.artifacts``) and log records (``core.logs``). A finding carries its type, a source
label and a line number, never the matched value.

Detectors, highest priority first (an overlap keeps the higher one):

- ``configured_key``: the resolved value of every ``env:`` reference that a configured
  provider's ``api_key`` uses, when the variable is set and at least 8 characters long.
  The value is held in memory only, never logged, stored or hashed.
- ``private_key``: ``-----BEGIN ... PRIVATE KEY-----`` through the next matching END line,
  or to the end of the text when there is no END line.
- Token formats: ``aws_access_key``, ``github_token``, ``slack_token``, ``google_api_key``,
  ``openai_style_key``, ``groq_key`` and ``jwt``. Each pattern refuses to start inside a
  longer word, so ``risk-assessment-for-the-module`` is not an ``sk-`` key.
- ``assigned_secret``: a value assigned to a name such as ``api_key``, ``secret``,
  ``token`` or ``password`` (``=``, ``:``, ``:=`` or ``=>``), at least 16 characters with
  Shannon entropy of at least 3.5 bits per character. Placeholders (``<...>``, ``${...}``,
  ``{{...}}``, ``env:...``, ``keychain:...``, one repeated character, runs of ``x`` or
  ``*``) are ignored, and so are unquoted code expressions: variables and templates
  (``$x``, ``@x``), calls (``(``, ``->``, ``::``) and dotted or indexed names
  (``settings.SECRET_KEY``, ``os.environ[...]``).

Repo config ``[repo] secret_scan_allow`` lists regexes for findings that are not secrets.
``configured_key`` and ``private_key`` findings are never allowlisted.

Scanning is linear in the input. Texts above 5 MiB are scanned in 5 MiB windows that
overlap by 1 KiB, so a token across a window border is still found. Configured keys and
private keys are searched in the whole text, so a key block longer than the overlap is
found too.
"""

import logging
import math
import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arpeggio_ai.config.models import Config

log = logging.getLogger(__name__)

CHUNK_CHARS = 5 * 1024 * 1024
OVERLAP_CHARS = 1024
MIN_CONFIGURED_KEY_CHARS = 8
MIN_ASSIGNED_CHARS = 16
MIN_ENTROPY_BITS = 3.5
MARKER = "[REDACTED:"
NEVER_ALLOWLISTED = frozenset({"configured_key", "private_key"})

_TOKENS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}")),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    ),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}")),
    ("openai_style_key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}")),
    ("groq_key", re.compile(r"(?<![A-Za-z0-9_])gsk_[A-Za-z0-9]{40,}")),
    (
        "jwt",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        ),
    ),
)
_PRIORITY = {
    name: rank
    for rank, name in enumerate(
        ["configured_key", "private_key", *(name for name, _ in _TOKENS), "assigned_secret"]
    )
}
_PK_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PK_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
# A name, an assignment operator and a quoted or bare value. The name is matched whole and
# possessively, then checked for a secret-like word in Python, which keeps this linear.
_ASSIGNED = re.compile(
    r"(?<![\w.-])(?P<name>[\w.-]++)[\"']?[ \t]*+(?::=|=>|[:=])[ \t]*+"
    r"(?:(?P<q>[\"'])(?P<quoted>[^\"'\n]{1,512})(?P=q)"
    r"|(?P<bare>(?:\[[^\]\n]{0,256}\]|[^\s\"'`,;\[]){1,512}))"
)
_SECRET_NAME = re.compile(
    r"api[_-]?key|secret|token|passw(?:or)?d|access[_-]?key|client[_-]?secret", re.IGNORECASE
)
_TEMPLATE_PREFIXES = ("${", "{{", "{%")
_EXPRESSION_PREFIXES = ("$", "@", *_TEMPLATE_PREFIXES)
_CALL_MARKERS = ("->", "::", "(")
_NAME_CHAIN = re.compile(r"[A-Za-z_][\w.]*(?:\[[^\]]*\])*")
_MASK_RUN = re.compile(r"[xX*]{4,}")


@dataclass(frozen=True, slots=True)
class Finding:
    type: str
    source: str
    line: int  # 1-based line where the finding starts


@dataclass(frozen=True, slots=True)
class ScanResult:
    text: str  # the input with every finding replaced
    findings: tuple[Finding, ...]

    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(finding.type for finding in self.findings).items()))


def shannon_entropy(value: str) -> float:
    """Bits per character of ``value``'s character distribution."""
    if not value:
        return 0.0
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in Counter(value).values())


def is_placeholder(value: str) -> bool:
    return (
        (value.startswith("<") and value.endswith(">"))
        or value.startswith(_TEMPLATE_PREFIXES)
        or value.startswith(("env:", "keychain:"))
        or len(set(value)) == 1
        or _MASK_RUN.search(value) is not None
    )


def is_expression(value: str) -> bool:
    """True if an unquoted value is code (a variable, template, call or dotted name)."""
    if value.startswith(_EXPRESSION_PREFIXES) or any(m in value for m in _CALL_MARKERS):
        return True
    # A dotted or indexed name. A single bare word is kept as a candidate: in .env, YAML
    # and INI files that is exactly how a secret is written.
    return _NAME_CHAIN.fullmatch(value) is not None and ("." in value or "[" in value)


def _is_assigned_secret(value: str, quoted: bool) -> bool:
    if len(value) < MIN_ASSIGNED_CHARS or is_placeholder(value):
        return False
    if not quoted and is_expression(value):
        return False
    return shannon_entropy(value) >= MIN_ENTROPY_BITS


def _windows(length: int) -> Iterator[tuple[int, int]]:
    if length <= CHUNK_CHARS:
        yield 0, length
        return
    for start in range(0, length, CHUNK_CHARS):
        yield start, min(start + CHUNK_CHARS + OVERLAP_CHARS, length)


def _private_keys(text: str) -> Iterator[tuple[int, int]]:
    position = 0
    while (begin := _PK_BEGIN.search(text, position)) is not None:
        end = _PK_END.search(text, begin.end())
        if end is None:
            yield begin.start(), len(text)
            return
        yield begin.start(), end.end()
        position = end.end()


class SecretScanner:
    """Detectors plus the configured keys and allowlist of one config."""

    def __init__(self, configured: Iterable[str] = (), allow: Iterable[str] = ()) -> None:
        values = {value for value in configured if len(value) >= MIN_CONFIGURED_KEY_CHARS}
        self._configured = tuple(sorted(values, key=len, reverse=True))
        self._allow = tuple(re.compile(pattern) for pattern in allow)

    def __repr__(self) -> str:  # never shows a configured value
        return f"SecretScanner(configured={len(self._configured)}, allow={len(self._allow)})"

    def _candidates(self, text: str) -> set[tuple[int, int, str]]:
        found: set[tuple[int, int, str]] = set()
        for value in self._configured:
            start = text.find(value)
            while start != -1:
                found.add((start, start + len(value), "configured_key"))
                start = text.find(value, start + len(value))
        found.update((start, end, "private_key") for start, end in _private_keys(text))
        for low, high in _windows(len(text)):
            for name, pattern in _TOKENS:
                found.update((m.start(), m.end(), name) for m in pattern.finditer(text, low, high))
            for match in _ASSIGNED.finditer(text, low, high):
                if _SECRET_NAME.search(match.group("name")) is None:
                    continue
                group = "quoted" if match.group("quoted") is not None else "bare"
                if _is_assigned_secret(match.group(group), quoted=group == "quoted"):
                    found.add((match.start(group), match.end(group), "assigned_secret"))
        return found

    def _allowed(self, text: str, start: int, end: int, kind: str) -> bool:
        if kind in NEVER_ALLOWLISTED:
            return False
        value = text[start:end]
        return any(pattern.fullmatch(value) for pattern in self._allow)

    def scan(self, text: str, source: str) -> ScanResult:
        """Return ``text`` with every finding redacted, and the findings."""
        candidates = [span for span in self._candidates(text) if not self._allowed(text, *span)]
        if not candidates:
            return ScanResult(text, ())
        # Higher priority first, then earlier, then longer. Keep what does not overlap.
        candidates.sort(key=lambda span: (_PRIORITY[span[2]], span[0], span[0] - span[1]))
        starts: list[int] = []
        kept: list[tuple[int, int, str]] = []
        for start, end, kind in candidates:
            index = bisect_left(starts, start)
            if index > 0 and kept[index - 1][1] > start:
                continue
            if index < len(kept) and kept[index][0] < end:
                continue
            starts.insert(index, start)
            kept.insert(index, (start, end, kind))
        pieces: list[str] = []
        findings: list[Finding] = []
        position, line = 0, 1
        for start, end, kind in kept:
            line += text.count("\n", position, start)
            pieces.append(text[position:start])
            pieces.append(f"{MARKER}{kind}]")
            findings.append(Finding(kind, source, line))
            line += text.count("\n", start, end)
            position = end
        pieces.append(text[position:])
        return ScanResult("".join(pieces), tuple(findings))

    def scan_logged(self, text: str, source: str) -> ScanResult:
        """``scan`` and log ``secret_scan.redacted`` when anything was found."""
        result = self.scan(text, source)
        if result.findings:
            log.warning("secret_scan.redacted", extra=event_fields(result, source))
        return result

    def redact(self, text: str, source: str) -> str:
        """The redacted text of ``scan_logged``."""
        return self.scan_logged(text, source).text


def event_fields(result: ScanResult, source: str) -> dict[str, object]:
    """Fields of a ``secret_scan.redacted`` event: counts per type and the source, no values."""
    return {"source": source, "redacted": len(result.findings), "types": result.counts()}


def configured_values(config: "Config", env: Mapping[str, str]) -> list[str]:
    """Values of the ``env:`` variables that provider ``api_key`` references, when set."""
    values = []
    for provider in config.providers.values():
        if provider.api_key is not None and provider.api_key.startswith("env:"):
            value = env.get(provider.api_key.removeprefix("env:"))
            if value:
                values.append(value)
    return values


def scanner_from_config(config: "Config", env: Mapping[str, str]) -> SecretScanner:
    return SecretScanner(configured_values(config, env), config.repo.secret_scan_allow)


DEFAULT_SCANNER = SecretScanner()
_current: ContextVar[SecretScanner | None] = ContextVar("arpeggio_secret_scanner", default=None)


def current_scanner() -> SecretScanner:
    """The scanner installed by ``use_scanner``, or detectors without configured keys."""
    return _current.get() or DEFAULT_SCANNER


@contextmanager
def use_scanner(scanner: SecretScanner) -> Iterator[None]:
    """Make ``scanner`` the one artifact writes and log records use inside the block."""
    token = _current.set(scanner)
    try:
        yield
    finally:
        _current.reset(token)
