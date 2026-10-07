from pathlib import Path

import pytest


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
