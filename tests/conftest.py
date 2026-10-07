import tomllib
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def template_text() -> str:
    """The packaged example config, read the same way an installed package would."""
    resource = files("arpeggio_ai.config").joinpath("templates", "config.example.toml")
    return resource.read_text(encoding="utf-8")


@pytest.fixture
def config_data(template_text: str) -> dict[str, Any]:
    """A fresh, valid raw config dict that a test may mutate."""
    return tomllib.loads(template_text)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ARPEGGIO_HOME at a temp dir for every test, so the real ~/.arpeggio is never used."""
    path = tmp_path / "arpeggio-home"
    monkeypatch.setenv("ARPEGGIO_HOME", str(path))
    return path


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def write_global(home: Path) -> Callable[[str], Path]:
    """Write text to <home>/config.toml and return its path."""

    def write(text: str) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        path = home / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def write_repo(repo_dir: Path) -> Callable[[str], Path]:
    """Write text to <repo>/.arpeggio/config.toml and return its path."""

    def write(text: str) -> Path:
        path = repo_dir / ".arpeggio" / "config.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    return write
