"""Filesystem locations: the Arpeggio home directory and config files."""

import os
from pathlib import Path

HOME_ENV_VAR = "ARPEGGIO_HOME"
CONFIG_FILENAME = "config.toml"
DB_FILENAME = "arpeggio.db"
DOT_DIRNAME = ".arpeggio"
RUNTIME_SUBDIRS: tuple[str, ...] = ("artifacts", "worktrees", "taste", "skills", "logs")


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


def repo_config_path(repo: Path) -> Path:
    """Return ``<repo>/.arpeggio/config.toml`` as an absolute path."""
    return repo.expanduser().absolute() / DOT_DIRNAME / CONFIG_FILENAME
