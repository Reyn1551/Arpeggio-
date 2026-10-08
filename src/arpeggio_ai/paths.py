"""Filesystem locations: the Arpeggio home directory and config files."""

import os
from pathlib import Path

HOME_ENV_VAR = "ARPEGGIO_HOME"
EVALS_ENV_VAR = "ARPEGGIO_EVALS_DIR"
CONFIG_FILENAME = "config.toml"
DB_FILENAME = "arpeggio.db"
DOT_DIRNAME = ".arpeggio"
RUNTIME_SUBDIRS: tuple[str, ...] = (
    "artifacts",
    "worktrees",
    "taste",
    "skills",
    "logs",
    "backups",
)


def arpeggio_home() -> Path:
    """Return ``$ARPEGGIO_HOME`` if set and non-empty, else ``~/.arpeggio``, as an absolute path."""
    value = os.environ.get(HOME_ENV_VAR, "").strip()
    home = Path(value).expanduser() if value else Path.home() / DOT_DIRNAME
    return home.absolute()


def global_config_path(home: Path | None = None) -> Path:
    """Return ``<home>/config.toml``."""
    return (home if home is not None else arpeggio_home()) / CONFIG_FILENAME


def db_path(home: Path | None = None) -> Path:
    """Return ``<home>/arpeggio.db``."""
    return (home if home is not None else arpeggio_home()) / DB_FILENAME


def artifacts_dir(home: Path | None = None) -> Path:
    """Return ``<home>/artifacts``."""
    return (home if home is not None else arpeggio_home()) / "artifacts"


def logs_dir(home: Path | None = None) -> Path:
    """Return ``<home>/logs``."""
    return (home if home is not None else arpeggio_home()) / "logs"


def repo_config_path(repo: Path) -> Path:
    """Return ``<repo>/.arpeggio/config.toml`` as an absolute path."""
    return repo.expanduser().absolute() / DOT_DIRNAME / CONFIG_FILENAME


def evals_dir(configured: str | None = None, home: Path | None = None) -> Path:
    """The personal eval suite: ``$ARPEGGIO_EVALS_DIR``, else ``[evals] dir``, else
    ``<home>/evals``. It lives outside the Arpeggio repo because its tasks come from private
    repositories (EVL-01)."""
    value = os.environ.get(EVALS_ENV_VAR, "").strip() or (configured or "").strip()
    if value:
        return Path(value).expanduser().absolute()
    return (home if home is not None else arpeggio_home()) / "evals"
