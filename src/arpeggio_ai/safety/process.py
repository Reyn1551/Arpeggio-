"""Run a check command with a scrubbed environment, a timeout and a process-tree kill.

Used for done-criteria checks and for every git command Arpeggio runs in a worktree.

- **Environment (SAF-02).** A child starts from an allowlist, never from ``os.environ``:
  ``ENV_ALLOWLIST`` plus the repo's ``check_env`` names, minus every variable a provider
  ``api_key`` references. Names compare case-insensitively on Windows.
- **Executable.** ``argv[0]`` is looked up on the scrubbed ``PATH``, so the PATH the child
  sees decides what runs. Windows ``.bat`` and ``.cmd`` files need a shell and are refused.
- **Output.** stdout and stderr go to one temporary file, in order. A killed process tree
  can never leave a pipe read hanging. At most ``MAX_OUTPUT_BYTES`` (10 MB) is kept: longer
  output keeps its last 10 MB behind a truncation marker. While the process runs, the file
  size is checked every ``POLL_S`` seconds, and past ``MAX_TEMP_OUTPUT_BYTES`` (100 MB) the
  tree is killed the same way as on timeout. The temporary file is deleted in every case.
  Both limits are module constants for now and become config later.
- **Timeout (EXE-06).** POSIX starts the child in a new session and kills its process
  group. Windows starts it in a new process group and runs ``taskkill /T /F``, which walks
  the tree by parent PID. A grandchild whose parent already exited is not reachable that
  way (a Job Object would be). See docs/07-SECURITY-AND-PRIVACY.md.
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
    executable = resolve_executable(argv[0], env, cwd)
    if sys.platform == "win32":
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
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
            timed_out, limit_exceeded = await _wait(process, output.fileno(), started, timeout_s)
            if timed_out or limit_exceeded:
                await _kill_tree(process.pid)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), REAP_WAIT_S)
            elif sys.platform != "win32":
                await _kill_tree(process.pid)  # background children left in the group
            duration = time.monotonic() - started
            data = _read_tail(output, MAX_OUTPUT_BYTES)
    finally:
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
