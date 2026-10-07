"""Exceptions Arpeggio raises on purpose."""

from dataclasses import asdict, dataclass


class ArpeggioError(Exception):
    """Base class for all errors raised by Arpeggio."""


@dataclass(frozen=True)
class ConfigIssue:
    """One config problem.

    ``file`` is the config file path, or ``"merged"`` when the problem is in the result of
    merging global and repo config. ``field`` is a dotted path such as
    ``models.tier2.mid.efforts``, or ``None`` when the problem concerns the whole file.
    """

    file: str
    field: str | None
    message: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)

    def __str__(self) -> str:
        where = f"{self.file}: {self.field}" if self.field else self.file
        return f"{where}: {self.message}"


class ConfigError(ArpeggioError):
    """Config is missing, unreadable, or invalid. Carries every issue found."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = list(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))
