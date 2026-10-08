"""Real child processes: exit codes, output, timeouts with tree kill, environment scrub."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from arpeggio_ai.safety import process as process_module
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


def test_output_over_the_cap_keeps_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_module, "MAX_OUTPUT_BYTES", 1000)
    script = "import sys; sys.stdout.write('a' * 5000 + 'END')"
    result = run([PY, "-c", script], tmp_path)
    assert result.output.startswith(
        b"[arpeggio: output truncated, kept the last 1000 of 5003 bytes]\n"
    )
    assert result.output.endswith(b"a" * 997 + b"END")
    assert (
        len(result.output)
        == len(b"[arpeggio: output truncated, kept the last 1000 of 5003 bytes]\n") + 1000
    )


def test_output_at_the_cap_is_kept_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_module, "MAX_OUTPUT_BYTES", 1000)
    result = run([PY, "-c", "import sys; sys.stdout.write('b' * 1000)"], tmp_path)
    assert result.output == b"b" * 1000


def test_default_cap_is_10_mb() -> None:
    assert process_module.MAX_OUTPUT_BYTES == 10 * 1024 * 1024


# Hard limit on the temporary output file


FLOOD_SCRIPT = """
import os, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as f:
    f.write(f"{os.getpid()} {child.pid}")
line = "x" * 999 + "\\n"
while True:
    sys.stdout.write(line)
"""


@pytest.fixture
def out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "check-output"
    path.mkdir()
    monkeypatch.setattr(process_module, "OUTPUT_TMP_DIR", path)
    return path


def test_flooding_check_is_killed_by_the_output_limit(
    tmp_path: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_module, "MAX_TEMP_OUTPUT_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(process_module, "MAX_OUTPUT_BYTES", 1000)
    monkeypatch.setattr(process_module, "POLL_S", 0.1)
    pids = tmp_path / "pids.txt"
    script = tmp_path / "flood.py"
    script.write_text(FLOOD_SCRIPT)
    started = time.monotonic()
    result = run([PY, str(script), str(pids)], tmp_path, timeout_s=60)
    assert time.monotonic() - started < 30, "the size limit should fire long before the timeout"
    assert (result.output_limit_exceeded, result.timed_out, result.exit_code) == (True, False, None)
    assert result.output.startswith(b"[arpeggio: output truncated, kept the last 1000 of ")
    assert result.output.rstrip().endswith(b"x")  # Windows text-mode stdout writes \r\n
    parent, child = (int(value) for value in pids.read_text().split())
    assert wait_gone(parent), "parent survived the output limit"
    assert wait_gone(child), "child survived the output limit"
    assert list(out_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("code", "timeout_s"),
    [("print('ok')", 30), ("raise SystemExit(2)", 30), ("import time; time.sleep(30)", 1)],
    ids=["pass", "fail", "timeout"],
)
def test_temp_output_is_deleted(tmp_path: Path, out_dir: Path, code: str, timeout_s: int) -> None:
    result = run([PY, "-c", code], tmp_path, timeout_s=timeout_s)
    assert result.output_limit_exceeded is False
    assert list(out_dir.iterdir()) == []


def test_temp_output_is_deleted_when_the_process_cannot_start(
    tmp_path: Path, out_dir: Path
) -> None:
    with pytest.raises(ProcessError, match="cannot start"):
        run([PY, "-c", "print(1)"], tmp_path / "missing-cwd")
    assert list(out_dir.iterdir()) == []


def test_default_temp_limit_is_100_mb() -> None:
    assert process_module.MAX_TEMP_OUTPUT_BYTES == 100 * 1024 * 1024
    assert process_module.POLL_S == 0.5


# Windows Job Object (EXE-06)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows only")

# The parent starts a middle process that starts a grandchild and exits at once, so the
# grandchild's parent is gone: taskkill /T cannot reach it by parent PID, a job can.
ORPHAN_SCRIPT = """
import subprocess, sys
middle = (
    "import subprocess, sys; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
    "open(sys.argv[1], 'w').write(str(child.pid))"
)
subprocess.run([sys.executable, "-c", middle, sys.argv[1]], check=True)
mode = sys.argv[2]
if mode == "sleep":
    import time
    time.sleep(120)
elif mode == "flood":
    line = "x" * 999 + "\\n"
    while True:
        sys.stdout.write(line)
"""


def orphan(tmp_path: Path, mode: str, **kwargs: float) -> tuple[ProcessResult, int]:
    script = tmp_path / "orphan.py"
    script.write_text(ORPHAN_SCRIPT)
    pid_file = tmp_path / "grandchild.pid"
    result = run([PY, str(script), str(pid_file), mode], tmp_path, **kwargs)
    assert pid_file.exists(), result.text()
    return result, int(pid_file.read_text())


@windows_only
def test_job_kills_an_orphaned_grandchild_on_timeout(tmp_path: Path) -> None:
    result, grandchild = orphan(tmp_path, "sleep", timeout_s=5)
    assert result.timed_out
    assert wait_gone(grandchild), "grandchild of an exited parent survived the timeout"


@windows_only
def test_job_kills_an_orphaned_grandchild_on_the_output_limit(
    tmp_path: Path, out_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_module, "MAX_TEMP_OUTPUT_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(process_module, "POLL_S", 0.1)
    result, grandchild = orphan(tmp_path, "flood", timeout_s=60)
    assert result.output_limit_exceeded and not result.timed_out
    assert wait_gone(grandchild), "grandchild survived the output limit"
    assert list(out_dir.iterdir()) == []


@windows_only
def test_job_kills_stragglers_after_a_normal_exit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="arpeggio_ai.safety.process"):
        result, grandchild = orphan(tmp_path, "exit", timeout_s=30)
    assert (result.exit_code, result.timed_out) == (0, False)
    assert wait_gone(grandchild), "background grandchild outlived the check"
    [record] = [r for r in caplog.records if r.getMessage() == "process.stragglers_killed"]
    assert record.__dict__["processes"] >= 1


@windows_only
def test_clean_exit_logs_no_stragglers(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger="arpeggio_ai.safety.process"):
        result = run([PY, "-c", "print('ok')"], tmp_path)
    assert result.exit_code == 0
    assert [r for r in caplog.records if r.getMessage() == "process.stragglers_killed"] == []


@windows_only
def test_child_starts_inside_the_job(tmp_path: Path) -> None:
    script = (
        "import ctypes; r = ctypes.c_int(); "
        "ctypes.windll.kernel32.IsProcessInJob(ctypes.c_void_p(-1), "
        "None, ctypes.byref(r)); print(r.value)"
    )
    assert run([PY, "-c", script], tmp_path).text().strip() == "1"


TREE_SCRIPT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


@windows_only
def test_without_a_job_taskkill_still_kills_the_direct_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse() -> None:
        process_module._job_unavailable("create", 5)  # what a refused CreateJobObjectW does
        return None

    monkeypatch.setattr(process_module, "_win_new_job", refuse)
    monkeypatch.setattr(process_module, "_job_unavailable_logged", False)
    script = tmp_path / "tree.py"
    script.write_text(TREE_SCRIPT)
    with caplog.at_level("WARNING", logger="arpeggio_ai.safety.process"):
        first = run([PY, str(script), str(tmp_path / "child.pid")], tmp_path, timeout_s=3)
        run([PY, "-c", "print(1)"], tmp_path)
    assert first.timed_out
    assert wait_gone(int((tmp_path / "child.pid").read_text())), "taskkill missed the child"
    unavailable = [r for r in caplog.records if r.getMessage() == "process.job_object_unavailable"]
    assert len(unavailable) == 1, "logged once per run"
    assert unavailable[0].__dict__["fallback"] == "taskkill"


@windows_only
def test_refused_assignment_falls_back_and_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(process_module._kernel32, "AssignProcessToJobObject", lambda *_: 0)
    monkeypatch.setattr(process_module, "_job_unavailable_logged", False)
    script = tmp_path / "tree.py"
    script.write_text(TREE_SCRIPT)
    with caplog.at_level("WARNING", logger="arpeggio_ai.safety.process"):
        ran = run([PY, "-c", "print('ran')"], tmp_path)
        killed = run([PY, str(script), str(tmp_path / "child.pid")], tmp_path, timeout_s=3)
    assert (ran.exit_code, ran.text().strip()) == (0, "ran")  # resumed although not in a job
    assert killed.timed_out
    assert wait_gone(int((tmp_path / "child.pid").read_text())), "taskkill fallback missed it"
    [record] = [r for r in caplog.records if r.getMessage() == "process.job_object_unavailable"]
    assert record.__dict__["stage"] == "assign"


def test_posix_never_creates_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        job = process_module.create_job()
        assert job is not None
        job.close()
        assert job.handle is None
    else:
        assert process_module.create_job() is None
