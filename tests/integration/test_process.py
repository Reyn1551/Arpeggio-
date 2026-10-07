"""Real child processes: exit codes, output, timeouts with tree kill, environment scrub."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from arpeggio_ai.safety.process import ProcessError, ProcessResult, run_process, scrubbed_env

PY = sys.executable


def run(
    argv: list[str], cwd: Path, *, timeout_s: float = 30, env: dict[str, str] | None = None
) -> ProcessResult:
    return asyncio.run(
        run_process(argv, cwd=cwd, env=scrubbed_env() if env is None else env, timeout_s=timeout_s)
    )


def is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of an exited parent may linger as a zombie until init reaps it.
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True
    return "State:\tZ" not in status


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    return not is_alive(pid)


def test_exit_codes_and_combined_output(tmp_path: Path) -> None:
    ok = run([PY, "-c", "print('out')"], tmp_path)
    assert (ok.exit_code, ok.timed_out, ok.text().strip()) == (0, False, "out")
    failed = run(
        [
            PY,
            "-c",
            "import sys; print('a'); sys.stdout.flush(); print('b', file=sys.stderr); sys.exit(3)",
        ],
        tmp_path,
    )
    assert failed.exit_code == 3
    assert failed.text().split() == ["a", "b"]
    assert failed.duration_s >= 0


def test_runs_in_the_given_directory(tmp_path: Path) -> None:
    result = run([PY, "-c", "import os; print(os.getcwd())"], tmp_path)
    assert Path(result.text().strip()).resolve() == tmp_path.resolve()


CHILD_SCRIPT = """
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as f:
    f.write(f"{os.getpid()} {child.pid}")
time.sleep(120)
"""


def test_timeout_kills_the_process_and_its_child(tmp_path: Path) -> None:
    pids = tmp_path / "pids.txt"
    script = tmp_path / "spawn.py"
    script.write_text(CHILD_SCRIPT)
    started = time.monotonic()
    result = run([PY, str(script), str(pids)], tmp_path, timeout_s=4)
    assert time.monotonic() - started < 20
    assert (result.timed_out, result.exit_code) == (True, None)
    parent, child = (int(value) for value in pids.read_text().split())
    assert wait_gone(parent), "parent survived the timeout"
    assert wait_gone(child), "child survived the timeout"


def test_unknown_executable_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProcessError, match="executable not found on the check PATH: no-such-tool"):
        run(["no-such-tool", "--version"], tmp_path)


def test_relative_executable_path_is_resolved_against_cwd(tmp_path: Path) -> None:
    with pytest.raises(ProcessError, match="executable not found"):
        run(["./missing-tool"], tmp_path)


def test_empty_argv_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProcessError, match="argv must not be empty"):
        run([], tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="batch files are a Windows concept")
def test_batch_files_are_refused_on_windows(tmp_path: Path) -> None:
    (tmp_path / "tool.cmd").write_text("@echo hi\r\n")
    env = scrubbed_env()
    env = {key: value for key, value in env.items() if key.upper() != "PATH"}
    env["PATH"] = str(tmp_path)
    with pytest.raises(ProcessError, match="batch file"):
        run(["tool"], tmp_path, env=env)


def test_environment_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("ARPEGGIO_CHECK_VISIBLE", "yes")
    monkeypatch.setenv("ARBITRARY_PARENT_VAR", "no")
    env = scrubbed_env(
        extra=["ARPEGGIO_CHECK_VISIBLE", "DEEPSEEK_API_KEY"], deny=["DEEPSEEK_API_KEY"]
    )
    result = run(
        [PY, "-c", "import json, os; print(json.dumps(dict(os.environ)))"], tmp_path, env=env
    )
    seen = json.loads(result.text())
    names = {name.upper() for name in seen}
    assert "DEEPSEEK_API_KEY" not in names
    assert "sk-must-not-leak" not in result.text()
    assert "ARBITRARY_PARENT_VAR" not in names
    assert seen.get("ARPEGGIO_CHECK_VISIBLE") == "yes"


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are a POSIX mechanism")
def test_posix_child_runs_in_its_own_process_group(tmp_path: Path) -> None:
    result = run([PY, "-c", "import os; print(os.getpgid(0) == os.getpid())"], tmp_path)
    assert result.text().strip() == "True"
