"""EXE-09: npm and npx on Windows run as node <npm-cli.js|npx-cli.js>; other .cmd refused."""

import asyncio
import os
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest

from arpeggio_ai.safety.process import ProcessError, resolve_argv, run_process, scrubbed_env

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="EXE-09 is Windows only")
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")


def path_env(directory: Path) -> dict[str, str]:
    env = {k: v for k, v in scrubbed_env().items() if k.upper() != "PATH"}
    env["PATH"] = str(directory)
    return env


def fake_node(root: Path, scripts: tuple[str, ...] = ("npm", "npx")) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    node = root / "node.exe"
    shutil.copyfile(sys.executable, node)
    bin_dir = root / "node_modules" / "npm" / "bin"
    bin_dir.mkdir(parents=True)
    for name in scripts:
        (bin_dir / f"{name}-cli.js").write_text("// fake\n", encoding="utf-8")
    (root / "npm.cmd").write_text("@echo shim\r\n", encoding="utf-8")
    (root / "other.cmd").write_text("@echo other\r\n", encoding="utf-8")
    return node


@WINDOWS
def test_npm_and_npx_resolve_to_node_and_the_cli_script(tmp_path: Path) -> None:
    node = fake_node(tmp_path / "nodejs")
    env = path_env(node.parent)
    bin_dir = node.parent / "node_modules" / "npm" / "bin"
    for name in ("npm", "NPX"):
        argv = resolve_argv([name, "--version"], env, tmp_path)
        assert Path(argv[0]).samefile(node)
        assert Path(argv[1]) == bin_dir / f"{name.lower()}-cli.js"
        assert argv[2:] == ["--version"]


@WINDOWS
def test_missing_cli_script_is_a_clear_error(tmp_path: Path) -> None:
    node = fake_node(tmp_path / "nodejs", scripts=("npm",))
    with pytest.raises(ProcessError, match=r"npx was requested but .*npx-cli\.js does not exist"):
        resolve_argv(["npx", "-v"], path_env(node.parent), tmp_path)


@WINDOWS
def test_npm_without_node_on_path_is_refused(tmp_path: Path) -> None:
    (tmp_path / "npm.cmd").write_text("@echo shim\r\n", encoding="utf-8")
    with pytest.raises(ProcessError, match="executable not found on the check PATH: node"):
        resolve_argv(["npm", "ci"], path_env(tmp_path), tmp_path)


@WINDOWS
def test_other_cmd_files_are_still_refused(tmp_path: Path) -> None:
    node = fake_node(tmp_path / "nodejs")
    env = path_env(node.parent)
    for name in ("other", "npm.cmd"):
        with pytest.raises(ProcessError, match="batch file"):
            resolve_argv([name], env, tmp_path)


@WINDOWS
def test_real_npm_version_runs_through_the_resolver(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    names = [name for name in ("APPDATA", "LOCALAPPDATA") if name in os.environ]
    env = scrubbed_env(extra=names)
    result = asyncio.run(run_process(["npm", "--version"], cwd=tmp_path, env=env, timeout_s=120))
    assert result.exit_code == 0, result.text()
    assert re.search(r"\d+\.\d+\.\d+", result.text())


@POSIX
def test_posix_npm_is_an_ordinary_executable(tmp_path: Path) -> None:
    tool = tmp_path / "npm"
    tool.write_text("#!/bin/sh\necho npm\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    assert resolve_argv(["npm", "ci"], path_env(tmp_path), tmp_path) == [str(tool), "ci"]
