"""Shared bounded subprocess execution for trusted fixed command adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO


class ProcessStatus(str, Enum):
    """Low-level outcome of one controlled child process."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ProcessCommand:
    """A fixed argument array and its local execution limits."""

    argv: tuple[str, ...]
    working_directory: Path
    timeout_seconds: int
    stdin: bytes | None = field(default=None, repr=False)
    environment_overrides: tuple[tuple[str, str], ...] = field(
        default=(),
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded output and status returned by a controlled child process."""

    status: ProcessStatus
    return_code: int | None
    duration_ms: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool

    @property
    def success(self) -> bool:
        return self.status is ProcessStatus.PASSED


def run_process(
    command: ProcessCommand,
    *,
    maximum_output_bytes: int,
) -> ProcessResult:
    """Run one fixed command without a shell and with bounded captured output."""

    started = time.monotonic_ns()
    environment = None
    if command.environment_overrides:
        environment = os.environ.copy()
        environment.update(command.environment_overrides)
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_stream,
        tempfile.TemporaryFile(mode="w+b") as stderr_stream,
    ):
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=command.working_directory,
                stdin=(
                    subprocess.PIPE if command.stdin is not None else subprocess.DEVNULL
                ),
                stdout=stdout_stream,
                stderr=stderr_stream,
                env=environment,
                shell=False,
                **_process_group_options(),
            )
        except OSError as exc:
            stderr, stderr_truncated = _bounded_text(
                str(exc).encode("utf-8", errors="replace"),
                maximum_output_bytes,
            )
            return _result(
                status=ProcessStatus.ERROR,
                return_code=None,
                started=started,
                stdout="",
                stderr=stderr,
                stdout_truncated=False,
                stderr_truncated=stderr_truncated,
            )

        timed_out = False
        try:
            if command.stdin is None:
                return_code = process.wait(timeout=command.timeout_seconds)
            else:
                process.communicate(
                    input=command.stdin,
                    timeout=command.timeout_seconds,
                )
                return_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            return_code = None
            _terminate_process(process)
        except BaseException:
            _terminate_process(process)
            raise

        stdout, stdout_truncated = _read_stream(
            stdout_stream,
            maximum_output_bytes,
        )
        stderr, stderr_truncated = _read_stream(
            stderr_stream,
            maximum_output_bytes,
        )
        status = (
            ProcessStatus.TIMED_OUT
            if timed_out
            else ProcessStatus.PASSED if return_code == 0 else ProcessStatus.FAILED
        )
        return _result(
            status=status,
            return_code=return_code,
            started=started,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


def _result(
    *,
    status: ProcessStatus,
    return_code: int | None,
    started: int,
    stdout: str,
    stderr: str,
    stdout_truncated: bool,
    stderr_truncated: bool,
) -> ProcessResult:
    return ProcessResult(
        status=status,
        return_code=return_code,
        duration_ms=(time.monotonic_ns() - started) // 1_000_000,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _read_stream(stream: BinaryIO, maximum: int) -> tuple[str, bool]:
    stream.flush()
    stream.seek(0)
    return _bounded_text(stream.read(maximum + 1), maximum)


def _bounded_text(raw: bytes, maximum: int) -> tuple[str, bool]:
    truncated = len(raw) > maximum
    text = raw[:maximum].decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n"), truncated


def _process_group_options() -> dict[str, object]:
    if os.name == "nt":  # pragma: no cover - exercised by Windows CI
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _terminate_process(process) -> None:
    if process.poll() is not None:
        return
    _signal_process(process, force=False)
    try:
        process.wait(timeout=1)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    _signal_process(process, force=True)
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _signal_process(process, *, force: bool) -> None:
    if os.name != "nt":
        selected = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(process.pid, selected)
            return
        except OSError:
            pass
    operation = process.kill if force else process.terminate
    try:
        operation()
    except OSError:
        pass


__all__ = [
    "ProcessCommand",
    "ProcessResult",
    "ProcessStatus",
    "run_process",
]
