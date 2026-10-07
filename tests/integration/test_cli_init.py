import json
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from arpeggio_ai.cli.app import app
from arpeggio_ai.cli.commands import init as init_module
from arpeggio_ai.config.loader import example_config_bytes, load_config

runner = CliRunner()
SUBDIRS = ["artifacts", "worktrees", "taste", "skills", "logs"]


def init_json(*extra: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["init", "--json", *extra])
    assert result.stderr == ""
    return result.exit_code, json.loads(result.stdout)


def all_paths(home: Path) -> list[str]:
    return [str(home), *(str(home / name) for name in SUBDIRS), str(home / "config.toml")]


def test_init_creates_home_subdirs_and_config(home: Path) -> None:
    code, payload = init_json()

    assert code == 0
    assert payload == {
        "ok": True,
        "home": str(home),
        "created": all_paths(home),
        "skipped": [],
        "backup": None,
    }
    assert all((home / name).is_dir() for name in SUBDIRS)
    assert (home / "config.toml").read_bytes() == example_config_bytes()
    load_config()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_init_creates_dirs_with_0700_despite_umask(home: Path) -> None:
    previous = os.umask(0o277)
    try:
        code, _ = init_json()
    finally:
        os.umask(previous)
    assert code == 0
    for path in [home, *(home / name for name in SUBDIRS)]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_init_is_idempotent(home: Path) -> None:
    init_json()
    (home / "config.toml").write_text("# my edits\n", encoding="utf-8")

    code, payload = init_json()

    assert code == 0
    assert payload["created"] == []
    assert payload["skipped"] == all_paths(home)
    assert payload["backup"] is None
    assert (home / "config.toml").read_text(encoding="utf-8") == "# my edits\n"
    assert sorted(p.name for p in home.iterdir()) == sorted([*SUBDIRS, "config.toml"])


def test_force_backs_up_existing_config_and_writes_fresh_one(home: Path) -> None:
    init_json()
    (home / "config.toml").write_text("# my edits\n", encoding="utf-8")

    code, payload = init_json("--force")

    assert code == 0
    backup = Path(payload["backup"])
    assert backup.parent == home
    assert re.fullmatch(r"config\.toml\.bak\.\d{8}T\d{6}Z", backup.name)
    assert backup.read_text(encoding="utf-8") == "# my edits\n"
    assert (home / "config.toml").read_bytes() == example_config_bytes()
    assert payload["created"] == [str(home / "config.toml")]
    assert payload["skipped"] == all_paths(home)[:-1]


def test_force_without_existing_config_makes_no_backup(home: Path) -> None:
    code, payload = init_json("--force")
    assert code == 0
    assert payload["backup"] is None
    assert payload["created"] == all_paths(home)


def test_force_never_overwrites_an_existing_backup(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(init_module, "_utc_now", lambda: fixed)
    init_json()
    backup = home / "config.toml.bak.20261007T120000Z"
    backup.write_text("# older backup\n", encoding="utf-8")
    (home / "config.toml").write_text("# current\n", encoding="utf-8")

    result = runner.invoke(app, ["init", "--force", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "already exists" in payload["errors"][0]["message"]
    assert backup.read_text(encoding="utf-8") == "# older backup\n"
    assert (home / "config.toml").read_text(encoding="utf-8") == "# current\n"


def test_root_json_flag_applies_to_init(home: Path) -> None:
    result = runner.invoke(app, ["--json", "init"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["home"] == str(home)


def test_init_human_output(home: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert f"Arpeggio home: {home}" in result.stdout
    assert "created" in result.stdout
    assert "arpeggio config validate" in result.stdout

    again = runner.invoke(app, ["init"])
    assert "skipped" in again.stdout
    assert "created" not in again.stdout


def test_init_human_output_shows_backup(home: Path) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["init", "--force"])
    assert result.exit_code == 0
    assert "backup" in result.stdout
    assert ".bak." in result.stdout


@pytest.mark.parametrize("as_json", [True, False])
def test_home_that_is_a_file_is_an_unexpected_error(home: Path, as_json: bool) -> None:
    home.write_text("not a directory", encoding="utf-8")
    result = runner.invoke(app, ["init", *(["--json"] if as_json else [])])
    assert result.exit_code == 1
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["errors"][0]["file"] is None
    else:
        assert result.stdout == ""
        assert result.stderr.startswith("error: ")


def test_init_help() -> None:
    result = runner.invoke(app, ["init", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.stdout
    assert "--json" in result.stdout
