import json
import os
import re
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from arpeggio_ai.cli.app import app
from arpeggio_ai.cli.commands import init as init_module
from arpeggio_ai.config.loader import TEMPLATES, load_config, template_bytes, template_env_vars
from arpeggio_ai.core.logs import close_logging
from arpeggio_ai.store.db import open_db
from arpeggio_ai.store.repositories import create_task, ensure_repo

runner = CliRunner()
SUBDIRS = ["artifacts", "worktrees", "taste", "skills", "logs", "backups"]
FREE_ENV = {"GEMINI_API_KEY": False, "GROQ_API_KEY": False, "OPENROUTER_API_KEY": False}


@pytest.fixture(autouse=True)
def no_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide any real API keys on this machine, so env_expected is deterministic."""
    for name in TEMPLATES:
        for var in template_env_vars(name):
            monkeypatch.delenv(var, raising=False)


def init_json(*extra: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["init", "--json", *extra])
    assert result.stderr == ""
    return result.exit_code, json.loads(result.stdout)


def dir_paths(home: Path) -> list[str]:
    return [str(home), *(str(home / name) for name in SUBDIRS)]


def all_paths(home: Path) -> list[str]:
    return [*dir_paths(home), str(home / "config.toml"), str(home / "arpeggio.db")]


def schema_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    finally:
        conn.close()


def test_init_creates_home_subdirs_and_config(home: Path) -> None:
    code, payload = init_json()

    assert code == 0
    assert payload == {
        "ok": True,
        "home": str(home),
        "profile": "free",
        "created": all_paths(home),
        "skipped": [],
        "backup": None,
        "env_expected": FREE_ENV,
    }
    assert all((home / name).is_dir() for name in SUBDIRS)
    assert (home / "config.toml").read_bytes() == template_bytes("free")
    assert schema_version(home / "arpeggio.db") == 2
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
    assert sorted(p.name for p in home.iterdir()) == sorted(
        [*SUBDIRS, "config.toml", "arpeggio.db"]
    )


def test_force_backs_up_existing_config_and_writes_fresh_one(home: Path) -> None:
    init_json()
    (home / "config.toml").write_text("# my edits\n", encoding="utf-8")

    code, payload = init_json("--force")

    assert code == 0
    backup = Path(payload["backup"])
    assert backup.parent == home
    assert re.fullmatch(r"config\.toml\.bak\.\d{8}T\d{6}Z", backup.name)
    assert backup.read_text(encoding="utf-8") == "# my edits\n"
    assert (home / "config.toml").read_bytes() == template_bytes("free")
    assert payload["created"] == [str(home / "config.toml")]
    assert payload["skipped"] == [*dir_paths(home), str(home / "arpeggio.db")]


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


# Database (OBS-01) and logs (OBS-03)


def test_deleting_the_database_and_rerunning_init_gives_a_working_empty_system(home: Path) -> None:
    init_json()
    conn = open_db(home / "arpeggio.db")
    ensure_repo(conn, "/code/app", "app")
    conn.close()

    (home / "arpeggio.db").unlink()
    code, payload = init_json()

    assert code == 0
    assert payload["created"] == [str(home / "arpeggio.db")]
    conn = open_db(home / "arpeggio.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
        repo = ensure_repo(conn, "/code/app", "app")
        assert create_task(conn, repo.id, "t", "t").status == "intake"
    finally:
        conn.close()


def test_force_keeps_existing_database_rows(home: Path) -> None:
    init_json()
    conn = open_db(home / "arpeggio.db")
    ensure_repo(conn, "/code/app", "app")
    conn.close()

    code, payload = init_json("--force")

    assert code == 0
    assert str(home / "arpeggio.db") in payload["skipped"]
    conn = open_db(home / "arpeggio.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_writes_json_lines_log(home: Path) -> None:
    init_json()
    init_json()
    close_logging()
    (log_file,) = (home / "logs").iterdir()
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in entries] == ["store.migrated", "init.completed", "init.completed"]
    assert entries[0]["from_version"] == 0
    assert entries[0]["to_version"] == 2
    assert entries[1]["created_count"] == 9
    assert entries[2]["skipped_count"] == 9


def test_newer_database_schema_is_an_error(home: Path) -> None:
    init_json()
    conn = sqlite3.connect(home / "arpeggio.db")
    conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, 'x')")
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["init", "--json"])

    assert result.exit_code == 1
    message = json.loads(result.stdout)["errors"][0]["message"]
    assert message.startswith("StoreError: database schema version 99 is newer")


# Profiles (CLI-05)


@pytest.mark.parametrize("profile", ["free", "micro-deepseek", "standard", "pro"])
def test_profile_writes_matching_template(home: Path, profile: str) -> None:
    code, payload = init_json("--profile", profile)
    assert code == 0
    assert payload["profile"] == profile
    assert (home / "config.toml").read_bytes() == template_bytes(profile)
    assert load_config().budget.profile == profile.split("-", 1)[0]


@pytest.mark.parametrize("as_json", [True, False])
def test_unknown_profile_exits_2_and_lists_valid_names(home: Path, as_json: bool) -> None:
    result = runner.invoke(app, ["init", "--profile", "cheap", *(["--json"] if as_json else [])])
    assert result.exit_code == 2
    expected = "unknown profile 'cheap'. Valid: free, micro-deepseek, standard, pro"
    if as_json:
        assert json.loads(result.stdout) == {
            "ok": False,
            "errors": [{"file": "command line", "field": "--profile", "message": expected}],
        }
    else:
        assert result.stdout == ""
        assert expected in result.stderr
    assert not home.exists()


def test_env_expected_reports_names_only(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "value-that-must-not-be-printed"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    code, payload = init_json("--profile", "micro-deepseek")
    assert code == 0
    assert payload["env_expected"] == {"DEEPSEEK_API_KEY": True}

    human = runner.invoke(app, ["init", "--profile", "micro-deepseek"])
    assert "DEEPSEEK_API_KEY  set" in human.stdout
    assert secret not in human.stdout + human.stderr


def test_empty_env_var_counts_as_not_set(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    _, payload = init_json()
    assert payload["env_expected"] == {**FREE_ENV, "GROQ_API_KEY": True}


def test_human_output_names_profile_and_missing_keys(home: Path) -> None:
    result = runner.invoke(app, ["init", "--profile", "standard"])
    assert result.exit_code == 0
    assert "Profile template: standard" in result.stdout
    assert "ANTHROPIC_API_KEY  not set" in result.stdout
    assert "DEEPSEEK_API_KEY  not set" in result.stdout


def test_existing_config_is_kept_when_profile_differs(home: Path) -> None:
    init_json("--profile", "standard")
    result = runner.invoke(app, ["init", "--profile", "micro-deepseek"])
    assert result.exit_code == 0
    assert "config.toml already exists and was kept" in result.stdout
    assert (home / "config.toml").read_bytes() == template_bytes("standard")


def test_force_switches_profile_and_keeps_database(home: Path) -> None:
    init_json()
    conn = open_db(home / "arpeggio.db")
    ensure_repo(conn, "/code/app", "app")
    conn.close()

    code, payload = init_json("--profile", "micro-deepseek", "--force")

    assert code == 0
    assert payload["profile"] == "micro-deepseek"
    assert Path(payload["backup"]).read_bytes() == template_bytes("free")
    assert (home / "config.toml").read_bytes() == template_bytes("micro-deepseek")
    assert payload["env_expected"] == {"DEEPSEEK_API_KEY": False}
    assert str(home / "arpeggio.db") in payload["skipped"]
    conn = open_db(home / "arpeggio.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 1
    finally:
        conn.close()
