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


class StoreError(ArpeggioError):
    """The database or artifact store cannot be used as asked."""


class ConfigError(ArpeggioError):
    """Config is missing, unreadable, or invalid. Carries every issue found."""

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = list(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


class SecretNotFound(ArpeggioError):
    """A secret reference points at nothing, for example an unset environment variable."""


class SpendRefused(ArpeggioError):
    """The spend guard refused a model call before any request was sent."""


class AdapterError(ArpeggioError):
    """An adapter was used in a way it does not support."""
