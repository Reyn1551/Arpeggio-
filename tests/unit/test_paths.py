from pathlib import Path

import pytest

from arpeggio_ai import paths


@pytest.fixture
def fake_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the user's home directory (POSIX and Windows) to a temp dir."""
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    return user_home


def test_env_var_is_respected(home: Path) -> None:
    assert paths.arpeggio_home() == home
    assert paths.arpeggio_home().is_absolute()


def test_relative_env_var_becomes_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARPEGGIO_HOME", "rel/home")
    assert paths.arpeggio_home() == tmp_path / "rel" / "home"


def test_tilde_in_env_var_is_expanded(
    fake_user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARPEGGIO_HOME", "~/custom")
    assert paths.arpeggio_home() == fake_user_home / "custom"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_falls_back_to_dot_arpeggio_in_user_home(
    value: str | None, fake_user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value is None:
        monkeypatch.delenv("ARPEGGIO_HOME", raising=False)
    else:
        monkeypatch.setenv("ARPEGGIO_HOME", value)
    assert paths.arpeggio_home() == fake_user_home / ".arpeggio"


def test_global_config_path(home: Path, tmp_path: Path) -> None:
    assert paths.global_config_path() == home / "config.toml"
    assert paths.global_config_path(tmp_path) == tmp_path / "config.toml"


def test_repo_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert paths.repo_config_path(Path("repo")) == tmp_path / "repo" / ".arpeggio" / "config.toml"


def test_runtime_subdirs() -> None:
    assert paths.RUNTIME_SUBDIRS == (
        "artifacts",
        "worktrees",
        "taste",
        "skills",
        "logs",
        "backups",
    )


def test_evals_dir_precedence(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.EVALS_ENV_VAR, raising=False)
    assert paths.evals_dir() == home / "evals"
    assert paths.evals_dir(str(tmp_path / "cfg")) == tmp_path / "cfg"
    monkeypatch.setenv(paths.EVALS_ENV_VAR, str(tmp_path / "env"))
    assert paths.evals_dir(str(tmp_path / "cfg")) == tmp_path / "env"
