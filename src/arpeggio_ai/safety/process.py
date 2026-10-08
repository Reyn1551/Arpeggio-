"""Run a check command with a scrubbed environment, a timeout and a process-tree kill.

Used for done-criteria checks and for every git command Arpeggio runs in a worktree.

- **Environment (SAF-02).** A child starts from an allowlist, never from ``os.environ``:
  ``ENV_ALLOWLIST`` plus the repo's ``check_env`` names, minus every variable a provider
  ``api_key`` references. Names compare case-insensitively on Windows.
- **Executable.** ``argv[0]`` is looked up on the scrubbed ``PATH``, so the PATH the child
  sees decides what runs. Windows ``.bat`` and ``.cmd`` files need a shell and are refused,
  except that on Windows ``npm`` and ``npx`` run as ``node <npm-cli.js|npx-cli.js>`` from
  the Node installation found on that PATH (EXE-09). npm usually needs ``APPDATA`` and
  ``LOCALAPPDATA`` in the repo's ``check_env``.
- **Output.** stdout and stderr go to one temporary file, in order. A killed process tree
  can never leave a pipe read hanging. At most ``MAX_OUTPUT_BYTES`` (10 MB) is kept: longer
  output keeps its last 10 MB behind a truncation marker. While the process runs, the file
  size is checked every ``POLL_S`` seconds, and past ``MAX_TEMP_OUTPUT_BYTES`` (100 MB) the
  tree is killed the same way as on timeout. The temporary file is deleted in every case.
  Both limits are module constants for now and become config later.
- **Timeout (EXE-06).** POSIX starts the child in a new session and kills its process
  group. Windows runs every child in its own Job Object with kill-on-close: the child
  starts suspended (``CREATE_SUSPENDED``), is assigned to the job through a handle from
  ``OpenProcess``, and only then resumes (``NtResumeProcess``), so no descendant can start
  outside the job. On timeout or output limit the job is terminated. After a normal exit
  anything still in the job is killed and logged as ``process.stragglers_killed``. If
  Windows refuses a job, ``process.job_object_unavailable`` is logged once per run and the
  older ``taskkill /T /F`` path is used, which cannot reach a grandchild whose parent
  already exited. See ADR-0008 (amendment) and docs/07-SECURITY-AND-PRIVACY.md.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from arpeggio_ai.config.models import Config
from arpeggio_ai.core.errors import ArpeggioError

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)
WINDOWS_ENV_ALLOWLIST: tuple[str, ...] = ("SYSTEMROOT", "COMSPEC", "PATHEXT")
SHELL_SCRIPT_SUFFIXES = frozenset({".bat", ".cmd"})
# On Windows these run as `node <npm dir>/bin/<name>-cli.js` instead of their .cmd shims.
NPM_COMMANDS = frozenset({"npm", "npx"})
REAP_WAIT_S = 5.0
# Output kept per run. Longer output keeps its last part, where test summaries are.
MAX_OUTPUT_BYTES = 10 * 1024 * 1024
# Hard limit on the temporary output file while a process runs. Past it the tree is killed.
MAX_TEMP_OUTPUT_BYTES = 100 * 1024 * 1024
POLL_S = 0.5
# Directory for temporary output files. None means the system temp directory.
OUTPUT_TMP_DIR: Path | None = None
TRUNCATED_MARKER = b"[arpeggio: output truncated, kept the last %d of %d bytes]\n"


class ProcessError(ArpeggioError):
    """A command could not be started, for example because its executable is missing."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int | None  # None when the process was killed (timeout or output limit)
    timed_out: bool
    duration_s: float
    output: bytes  # stdout and stderr combined, at most the last MAX_OUTPUT_BYTES
    output_limit_exceeded: bool = False

    def text(self) -> str:
        return self.output.decode("utf-8", errors="replace")


def provider_key_names(config: Config) -> set[str]:
    """Every environment variable a provider ``api_key`` references (``env:NAME``)."""
    return {
        provider.api_key.removeprefix("env:")
        for provider in config.providers.values()
        if provider.api_key is not None and provider.api_key.startswith("env:")
    }


def scrubbed_env(
    extra: Iterable[str] = (),
    deny: Iterable[str] = (),
    source: Mapping[str, str] | None = None,
    *,
    windows: bool = IS_WINDOWS,
) -> dict[str, str]:
    """The environment for a check: allowlist plus ``extra``, never anything in ``deny``."""

    def norm(name: str) -> str:
        return name.upper() if windows else name

    allowed = [*ENV_ALLOWLIST, *(WINDOWS_ENV_ALLOWLIST if windows else ()), *extra]
    wanted = {norm(name) for name in allowed} - {norm(name) for name in deny}
    values = os.environ if source is None else source
    return {name: value for name, value in values.items() if norm(name) in wanted}


def _env_path(env: Mapping[str, str]) -> str:
    for name, value in env.items():
        if (name.upper() if IS_WINDOWS else name) == "PATH":
            return value
    return ""


def resolve_executable(name: str, env: Mapping[str, str], cwd: Path) -> str:
    """Absolute path of ``argv[0]``: as given if it contains a separator, else from PATH."""
    if "/" in name or "\\" in name:
        candidate = Path(name) if Path(name).is_absolute() else cwd / name
        found = str(candidate) if candidate.is_file() else None
    else:
        found = shutil.which(name, path=_env_path(env))
    if found is None:
        raise ProcessError(f"executable not found on the check PATH: {name}")
    if IS_WINDOWS and Path(found).suffix.lower() in SHELL_SCRIPT_SUFFIXES:
        raise ProcessError(
            f"{name} resolves to a batch file ({Path(found).name}), which needs a shell;"
            " use an .exe or run the interpreter explicitly"
        )
    return found


def resolve_npm(name: str, env: Mapping[str, str], cwd: Path) -> list[str]:
    """Windows only (EXE-09): ``npm``/``npx`` as ``node <npm-cli.js|npx-cli.js>``.

    ``node.exe`` comes from the scrubbed PATH and the script from the npm that ships with
    that Node installation, so no ``.cmd`` shim and no shell is involved.
    """
    node = resolve_executable("node", env, cwd)
    script = Path(node).parent / "node_modules" / "npm" / "bin" / f"{name}-cli.js"
    if not script.is_file():
        raise ProcessError(
            f"{name} was requested but {script} does not exist; install npm with Node.js"
            " or run `node <path to npm-cli.js>` explicitly"
        )
    return [node, str(script)]


def resolve_argv(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> list[str]:
    """``argv`` with ``argv[0]`` resolved to an absolute executable (see module docstring)."""
    if IS_WINDOWS and argv[0].lower() in NPM_COMMANDS:
        return [*resolve_npm(argv[0].lower(), env, cwd), *argv[1:]]
    return [resolve_executable(argv[0], env, cwd), *argv[1:]]


async def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        taskkill = str(Path(root) / "System32" / "taskkill.exe")
        killer = await asyncio.create_subprocess_exec(
            taskkill,
            "/T",
            "/F",
            "/PID",
            str(pid),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)  # the child leads its own session and group


# Windows Job Objects (EXE-06). Every name below that touches ctypes.windll is only reached
# when sys.platform == "win32", so mypy --platform linux and POSIX runs never see it.

CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_ACCESS = 0x0001 | 0x0100 | 0x0200 | 0x0800 | 0x1000  # terminate, set quota,
# set information, suspend/resume, query limited information
_job_unavailable_logged = False

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long  # NTSTATUS, 0 on success

    def _win_new_job() -> int | None:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            _job_unavailable("create", ctypes.get_last_error())
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            _job_unavailable("configure", ctypes.get_last_error())
            _kernel32.CloseHandle(handle)
            return None
        return int(handle)

    def _win_adopt(job: int, pid: int) -> bool:
        # PID reuse is impossible here: the Popen object still holds a handle to the child.
        process = _kernel32.OpenProcess(_PROCESS_ACCESS, False, pid)
        if not process:
            error = ctypes.get_last_error()
            raise ProcessError(f"cannot open the started process: WinError {error}")
        try:
            assigned = bool(_kernel32.AssignProcessToJobObject(job, process))
            if not assigned:
                _job_unavailable("assign", ctypes.get_last_error())
            status = _ntdll.NtResumeProcess(process)
            if status != 0:
                _kernel32.TerminateProcess(process, 1)
                raise ProcessError(f"cannot resume the started process: NTSTATUS {status:#x}")
        finally:
            _kernel32.CloseHandle(process)
        return assigned

    def _win_active_processes(job: int) -> int:
        info = _BasicAccounting()
        if not _kernel32.QueryInformationJobObject(
            job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            return 0
        return int(info.ActiveProcesses)

    def _win_terminate(job: int) -> None:
        _kernel32.TerminateJobObject(job, 1)

    def _win_close(handle: int) -> None:
        _kernel32.CloseHandle(handle)

else:  # POSIX never creates a job, so these are never called with a real handle.

    def _win_new_job() -> int | None:
        return None

    def _win_adopt(job: int, pid: int) -> bool:
        return False

    def _win_active_processes(job: int) -> int:
        return 0

    def _win_terminate(job: int) -> None:
        return None

    def _win_close(handle: int) -> None:
        return None


def _job_unavailable(stage: str, error: int) -> None:
    """Log ``process.job_object_unavailable`` once per Arpeggio process."""
    global _job_unavailable_logged
    if not _job_unavailable_logged:
        _job_unavailable_logged = True
        log.warning(
            "process.job_object_unavailable",
            extra={"stage": stage, "winerror": error, "fallback": "taskkill"},
        )


class WindowsJob:
    """One check's Job Object (Windows only; never created on POSIX)."""

    def __init__(self, handle: int) -> None:
        self.handle: int | None = handle

    def adopt(self, pid: int) -> bool:
        """Put the suspended process ``pid`` in the job, then resume it.

        Returns False if the job refused it (the process still runs, outside the job).
        Raises ProcessError if the process cannot be resumed; it is terminated then.
        """
        assert self.handle is not None
        return _win_adopt(self.handle, pid)

    def active_processes(self) -> int:
        return 0 if self.handle is None else _win_active_processes(self.handle)

    def terminate(self) -> None:
        if self.handle is not None:
            _win_terminate(self.handle)

    def close(self) -> None:
        if self.handle is not None:
            _win_close(self.handle)
        self.handle = None


def create_job() -> WindowsJob | None:
    """A Job Object for the next check on Windows, None on POSIX or if Windows refuses one."""
    handle = _win_new_job()
    return None if handle is None else WindowsJob(handle)


async def _enter_job(job: WindowsJob, process: asyncio.subprocess.Process) -> bool:
    """Move a suspended child into ``job`` and resume it. False means: fall back to taskkill."""
    try:
        if job.adopt(process.pid):
            return True
    except ProcessError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), REAP_WAIT_S)
        raise
    job.close()
    return False


def _read_tail(handle: IO[bytes], limit: int) -> bytes:
    """The file's content, or its last ``limit`` bytes after a truncation marker."""
    size = handle.seek(0, os.SEEK_END)
    if size <= limit:
        handle.seek(0)
        return handle.read()
    handle.seek(size - limit)
    return TRUNCATED_MARKER % (limit, size) + handle.read()


async def run_process(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float
) -> ProcessResult:
    """Run ``argv`` in ``cwd`` with exactly ``env``.

    The whole process tree is killed after ``timeout_s``, or as soon as its output passes
    ``MAX_TEMP_OUTPUT_BYTES``.
    """
    if not argv:
        raise ProcessError("argv must not be empty")
    argv = resolve_argv(argv, env, cwd)
    executable = argv[0]
    job = create_job()
    if sys.platform == "win32":
        # In a job the child starts suspended and runs only once it is inside the job.
        suspended = CREATE_SUSPENDED if job is not None else 0
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | suspended}
    else:
        flags = {"start_new_session": True}
    fd, name = tempfile.mkstemp(prefix="arpeggio-check-", suffix=".log", dir=OUTPUT_TMP_DIR)
    try:
        with os.fdopen(fd, "w+b") as output:
            started = time.monotonic()
            try:
                process = await asyncio.create_subprocess_exec(
                    executable,
                    *argv[1:],
                    cwd=cwd,
                    env=dict(env),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    **flags,  # type: ignore[arg-type]
                )
            except OSError as error:
                raise ProcessError(f"cannot start {argv[0]}: {error.strerror or error}") from None
            if job is not None and not await _enter_job(job, process):
                job = None  # not in the job, but running: fall back to taskkill
            timed_out, limit_exceeded = await _wait(process, output.fileno(), started, timeout_s)
            if timed_out or limit_exceeded:
                if job is not None:
                    job.terminate()
                else:
                    await _kill_tree(process.pid)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), REAP_WAIT_S)
            elif job is not None:
                stragglers = job.active_processes()
                if stragglers:
                    job.terminate()
                    log.warning("process.stragglers_killed", extra={"processes": stragglers})
            elif sys.platform != "win32":
                await _kill_tree(process.pid)  # background children left in the group
            duration = time.monotonic() - started
            data = _read_tail(output, MAX_OUTPUT_BYTES)
    finally:
        if job is not None:
            job.close()  # kill-on-close also ends anything still in the job
        _delete(Path(name))
    killed = timed_out or limit_exceeded
    return ProcessResult(
        exit_code=None if killed else process.returncode,
        timed_out=timed_out,
        duration_s=round(duration, 3),
        output=data,
        output_limit_exceeded=limit_exceeded,
    )


async def _wait(
    process: asyncio.subprocess.Process, fd: int, started: float, timeout_s: float
) -> tuple[bool, bool]:
    """Wait for exit, polling the output size. Returns (timed_out, output_limit_exceeded)."""
    deadline = started + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True, False
        try:
            await asyncio.wait_for(process.wait(), min(POLL_S, remaining))
            return False, False
        except TimeoutError:
            if os.fstat(fd).st_size > MAX_TEMP_OUTPUT_BYTES:
                log.warning(
                    "process.output_limit_exceeded",
                    extra={"limit_bytes": MAX_TEMP_OUTPUT_BYTES, "pid": process.pid},
                )
                return False, True


def _delete(path: Path) -> None:
    """Remove a temporary output file. Retries briefly while Windows releases the handle."""
    for _ in range(20):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.1)
    log.warning("process.temp_output_not_deleted", extra={"path": str(path)})
