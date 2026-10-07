import tomllib
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
