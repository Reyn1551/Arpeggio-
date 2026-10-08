"""`arpeggio secrets scan`: a dry run that reports where findings are, never what (SAF-02)."""

import json
from pathlib import Path

import pytest
from gitrepo import make_repo
from typer.testing import CliRunner, Result

from arpeggio_ai.cli.app import app

runner = CliRunner()
FAKE_AWS_KEY = "AKIAQ3EGRYKUZLNVHM7X"
FAKE_GITHUB = "ghp_" + "Zq8Lr2Vx" * 5


def invoke(*args: str) -> Result:
    return runner.invoke(app, list(args))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    files = {
        ".gitignore": "ignored.txt\nbuild/\n",
        "app.py": f'KEY = "{FAKE_AWS_KEY}"\nOTHER = "{FAKE_GITHUB}"\n',
        "docs/notes.md": f"line one\nkey {FAKE_AWS_KEY}\n",
        "clean.py": "print('hello')\n",
    }
    path = make_repo(tmp_path / "repo", files)
    (path / "ignored.txt").write_text(f"{FAKE_AWS_KEY}\n")
    (path / "build").mkdir()
    (path / "build" / "out.txt").write_text(f"{FAKE_AWS_KEY}\n")
    (path / "untracked.py").write_text(f'token = "{FAKE_GITHUB}"\n')
    (path / "blob.bin").write_bytes(b"\0\1" + FAKE_AWS_KEY.encode())
    return path


def scan(*args: str) -> dict[str, object]:
    result = invoke("secrets", "scan", *args, "--json")
    assert result.exit_code == 0, result.stdout
    assert FAKE_AWS_KEY not in result.stdout and FAKE_GITHUB not in result.stdout
    data: dict[str, object] = json.loads(result.stdout)
    return data


def test_counts_respect_gitignore_and_skip_binaries(repo: Path) -> None:
    data = scan(str(repo))
    assert data["totals"] == {"aws_access_key": 2, "github_token": 2}
    files = {f["file"] for f in data["findings"]}  # type: ignore[attr-defined]
    assert files == {"app.py", "docs/notes.md", "untracked.py"}
    assert data["skipped"] == {"binary": 1}
    assert data["files_scanned"] == 5  # .gitignore, app.py, clean.py, notes.md, untracked.py
    assert data["top_files"][0] == {"file": "app.py", "count": 2}  # type: ignore[index]
    assert {"file": "docs/notes.md", "line": 2, "type": "aws_access_key"} in data["findings"]  # type: ignore[operator]
    for finding in data["findings"]:  # type: ignore[attr-defined]
        assert set(finding) == {"file", "line", "type"}


def test_include_and_exclude(repo: Path) -> None:
    only_docs = scan(str(repo), "--include", "docs/**")
    assert only_docs["totals"] == {"aws_access_key": 1}
    no_py = scan(str(repo), "--exclude", "**/*.py")
    assert no_py["totals"] == {"aws_access_key": 1}


def test_outside_git_scans_every_file(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "a.txt").write_text(f"{FAKE_AWS_KEY}\n")
    (folder / "big.txt").write_bytes(b"a" * (5 * 1024 * 1024 + 1))
    data = scan(str(folder))
    assert data["totals"] == {"aws_access_key": 1}
    assert data["skipped"] == {"too_large": 1}


def test_single_file(repo: Path) -> None:
    data = scan(str(repo / "app.py"))
    assert data["files_scanned"] == 1
    assert data["totals"] == {"aws_access_key": 1, "github_token": 1}


def test_repo_allowlist_is_used(repo: Path) -> None:
    (repo / ".arpeggio").mkdir()
    (repo / ".arpeggio" / "config.toml").write_text(
        f'[repo]\nsecret_scan_allow = ["{FAKE_AWS_KEY}"]\n'
    )
    totals = scan(str(repo))["totals"]
    assert isinstance(totals, dict)
    assert "aws_access_key" not in totals and totals["github_token"] == 2


def test_human_output_has_no_matched_text(repo: Path) -> None:
    result = invoke("secrets", "scan", str(repo))
    assert result.exit_code == 0
    assert FAKE_AWS_KEY not in result.stdout and FAKE_GITHUB not in result.stdout
    assert "aws_access_key" in result.stdout and "Top files" in result.stdout


def test_clean_tree_and_missing_path(tmp_path: Path) -> None:
    folder = tmp_path / "clean"
    folder.mkdir()
    (folder / "a.py").write_text("x = 1\n")
    assert "No findings." in invoke("secrets", "scan", str(folder)).stdout
    missing = invoke("secrets", "scan", str(tmp_path / "missing"), "--json")
    assert missing.exit_code == 1
    assert "path not found" in json.loads(missing.stdout)["errors"][0]["message"]


def test_help_states_the_purpose() -> None:
    result = invoke("secrets", "scan", "--help")
    assert "false positives" in result.stdout
