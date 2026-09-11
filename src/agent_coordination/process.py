"""Bounded subprocess execution: spawning, timing out, and bounding output.

Every process this package runs -- `gh`, `git`, tmux, and GNOME Terminal --
goes through this module, the sole `subprocess` importer, so timeout, output-size and I/O-failure
handling exist in exactly one place. Failures come back as typed,
provider-neutral exceptions; only a caller that knows what command it ran and
why can turn one into a user-facing message.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 60
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
_OUTPUT_CHUNK_BYTES = 64 * 1024
_PROCESS_EXIT_POLL_SECONDS = 1
_MAX_PROC_COMMAND_LINE_BYTES = 16 * 1024
_MAX_NATIVE_PROCESS_SCAN = 1024
_STAT_START_TIME_INDEX = 19


class NativeProcessState(StrEnum):
    LIVE = "live"
    ZOMBIE = "zombie"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    BOUNDED = "bounded"


@dataclass(frozen=True)
class NativeProcess:
    """A transient bounded `/proc` observation; command bytes never leave the caller."""

    pid: int
    state: NativeProcessState
    uid: int | None = None
    boot_id: str | None = None
    start_time: int | None = None
    comm: str | None = None
    directory: Path | None = None
    command_line: bytes | None = None


@dataclass(frozen=True)
class NativeProcessScan:
    processes: tuple[NativeProcess, ...]
    complete: bool


class IoStage(StrEnum):
    WAITING = "waiting for I/O"
    SENDING = "sending bounded input"
    READING = "reading output"
    COORDINATING = "coordinating I/O"


class ProcessError(RuntimeError):
    """A command could not be run to completion within its bounds."""


class ExecutableMissingError(ProcessError):
    def __init__(self, executable: str):
        self.executable = executable
        super().__init__(executable)


class ProcessStartFailedError(ProcessError):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class ProcessTimedOutError(ProcessError):
    pass


class ProcessIoFailedError(ProcessError):
    def __init__(self, stage: IoStage, detail: str):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage.value}: {detail}")


class ProcessOutputTooLargeError(ProcessError):
    pass


class ProcessDidNotExitError(ProcessError):
    pass


@dataclass(frozen=True)
class BoundedResult:
    """A finished bounded command: its exit status and merged stdout+stderr."""

    exit_status: int
    output: bytes


@dataclass(frozen=True)
class CapturedResult:
    """A finished captured command, with stdout and stderr kept separate."""

    exit_status: int
    stdout: bytes
    stderr: bytes


def _stop_process(process_handle: subprocess.Popen[bytes]) -> None:
    if process_handle.poll() is not None:
        return
    process_handle.terminate()
    try:
        process_handle.wait(timeout=_PROCESS_EXIT_POLL_SECONDS)
    except subprocess.TimeoutExpired:
        process_handle.kill()
        process_handle.wait(timeout=_PROCESS_EXIT_POLL_SECONDS)


def _close_process_streams(process_handle: subprocess.Popen[bytes]) -> None:
    for stream in (process_handle.stdin, process_handle.stdout, process_handle.stderr):
        if stream is None or stream.closed:
            continue
        with suppress(OSError, ValueError):
            stream.close()


def _start_bounded_process(
    command: list[str], *, env: dict[str, str] | None, input_data: bytes | None
) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
    except FileNotFoundError as error:
        raise ExecutableMissingError(command[0]) from error
    except OSError as error:
        raise ProcessStartFailedError(str(error)) from error


def _register_process_streams(
    selector: selectors.BaseSelector,
    process_handle: subprocess.Popen[bytes],
    input_data: bytes | None,
) -> memoryview | None:
    assert process_handle.stdout is not None
    selector.register(process_handle.stdout, selectors.EVENT_READ, "stdout")
    if input_data is None:
        return None
    assert process_handle.stdin is not None
    os.set_blocking(process_handle.stdin.fileno(), False)
    selector.register(process_handle.stdin, selectors.EVENT_WRITE, "stdin")
    return memoryview(input_data)


def _await_process_io_events(
    selector: selectors.BaseSelector,
    process_handle: subprocess.Popen[bytes],
    deadline: float,
) -> list[tuple[selectors.SelectorKey, int]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _stop_process(process_handle)
        raise ProcessTimedOutError
    try:
        events = selector.select(remaining)
    except OSError as error:
        raise ProcessIoFailedError(IoStage.WAITING, str(error)) from error
    if not events:
        _stop_process(process_handle)
        raise ProcessTimedOutError
    return events


def _write_process_input(
    key: selectors.SelectorKey,
    pending_input: memoryview,
    selector: selectors.BaseSelector,
    process_handle: subprocess.Popen[bytes],
) -> memoryview:
    # `key.fileobj` is typeshed's `int | HasFileno`, the general shape any selector
    # registration may carry; this module only ever registers `process_handle`'s own
    # streams (`_register_process_streams`), so the concretely typed stream comes
    # from there instead of narrowing the selector's wider protocol.
    stream = process_handle.stdin
    assert stream is not None
    try:
        written = os.write(stream.fileno(), pending_input)
    except BrokenPipeError:
        written = len(pending_input)
    except OSError as error:
        _stop_process(process_handle)
        raise ProcessIoFailedError(IoStage.SENDING, str(error)) from error
    remaining_input = pending_input[written:]
    if not remaining_input:
        selector.unregister(key.fileobj)
        stream.close()
    return remaining_input


def _read_process_output(
    key: selectors.SelectorKey,
    selector: selectors.BaseSelector,
    output: bytearray,
    process_handle: subprocess.Popen[bytes],
) -> None:
    stream = process_handle.stdout
    assert stream is not None
    try:
        chunk = os.read(stream.fileno(), _OUTPUT_CHUNK_BYTES)
    except OSError as error:
        raise ProcessIoFailedError(IoStage.READING, str(error)) from error
    if not chunk:
        selector.unregister(key.fileobj)
        return
    output.extend(chunk)
    if len(output) > MAX_COMMAND_OUTPUT_BYTES:
        _stop_process(process_handle)
        raise ProcessOutputTooLargeError


def _wait_for_process_exit(process_handle: subprocess.Popen[bytes]) -> int:
    try:
        return process_handle.wait(timeout=_PROCESS_EXIT_POLL_SECONDS)
    except subprocess.TimeoutExpired as error:
        _stop_process(process_handle)
        raise ProcessDidNotExitError from error


def _reap_bounded_process(
    selector: selectors.BaseSelector | None,
    process_handle: subprocess.Popen[bytes],
) -> None:
    try:
        if selector is not None:
            selector.close()
    except OSError:
        pass
    finally:
        _close_process_streams(process_handle)
        if process_handle.poll() is None:
            _stop_process(process_handle)


def _exchange_bounded_io(
    process_handle: subprocess.Popen[bytes], *, timeout: float, input_data: bytes | None
) -> tuple[bytes, int]:
    selector: selectors.BaseSelector | None = None
    try:
        deadline = time.monotonic() + timeout
        output = bytearray()
        selector = selectors.DefaultSelector()
        pending_input = _register_process_streams(selector, process_handle, input_data)
        while selector.get_map():
            events = _await_process_io_events(selector, process_handle, deadline)
            for key, _ in events:
                if key.data == "stdin":
                    assert pending_input is not None
                    pending_input = _write_process_input(
                        key, pending_input, selector, process_handle
                    )
                    continue
                _read_process_output(key, selector, output, process_handle)
        return bytes(output), _wait_for_process_exit(process_handle)
    except OSError as error:
        raise ProcessIoFailedError(IoStage.COORDINATING, str(error)) from error
    finally:
        _reap_bounded_process(selector, process_handle)


def run_bounded(
    command: list[str],
    *,
    input_data: bytes | None = None,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> BoundedResult:
    """Run `command` to completion, merging stdout and stderr, bounded by `timeout`
    and `MAX_COMMAND_OUTPUT_BYTES`.

    Raises a typed `ProcessError` subclass whenever the command cannot be run to
    completion. A nonzero exit status is not one of those failures -- it comes
    back as an ordinary `BoundedResult` for the caller to interpret, since only
    the caller knows what a nonzero exit from this particular command means.
    """
    process_handle = _start_bounded_process(command, env=env, input_data=input_data)
    output, exit_status = _exchange_bounded_io(
        process_handle, timeout=timeout, input_data=input_data
    )
    return BoundedResult(exit_status, output)


def run_captured(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> CapturedResult:
    """Run `command` to completion with stdout and stderr captured separately.

    Raises `ExecutableMissingError` or `ProcessTimedOutError`; a nonzero exit status
    comes back as an ordinary `CapturedResult`. Bytes are returned undecoded,
    universal-newline handling left to the caller, so this reproduces
    `subprocess.run(check=False, capture_output=True)` rather than duplicating
    the bounded I/O machinery `run_bounded` needs for streamed input.
    """
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, timeout=timeout, env=env
        )
    except FileNotFoundError as error:
        raise ExecutableMissingError(command[0]) from error
    except subprocess.TimeoutExpired as error:
        raise ProcessTimedOutError from error
    return CapturedResult(completed.returncode, completed.stdout, completed.stderr)


def inspect_native_process(pid: int, proc_root: Path = Path("/proc")) -> NativeProcess:
    """Read one Linux process twice-safe identity snapshot without shelling out.

    The result intentionally carries raw command bytes only long enough for the
    terminal boundary to classify them.  Callers persist a receipt, never argv.
    """
    identity = _process_identity(pid, proc_root)
    if identity.state is not NativeProcessState.LIVE:
        return identity
    return _live_process_snapshot(identity, proc_root / str(pid), proc_root)


def _process_identity(pid: int, proc_root: Path) -> NativeProcess:
    identity = _stat_identity(pid, proc_root)
    if identity.state not in {NativeProcessState.LIVE, NativeProcessState.ZOMBIE}:
        return identity
    try:
        uid = _proc_uid(proc_root / str(pid) / "status")
    except FileNotFoundError:
        return NativeProcess(pid, NativeProcessState.ABSENT)
    except (OSError, ValueError):
        return replace(identity, state=NativeProcessState.UNKNOWN)
    return replace(identity, uid=uid)


def _stat_identity(pid: int, proc_root: Path) -> NativeProcess:
    absent = NativeProcess(pid, NativeProcessState.ABSENT)
    if pid <= 0:
        return absent
    try:
        state, comm, start_time = _proc_stat(proc_root / str(pid) / "stat")
    except FileNotFoundError:
        return absent
    except (OSError, ValueError):
        return NativeProcess(pid, NativeProcessState.UNKNOWN)
    if state == "Z":
        return NativeProcess(pid, NativeProcessState.ZOMBIE, comm=comm, start_time=start_time)
    return NativeProcess(pid, NativeProcessState.LIVE, start_time=start_time, comm=comm)


def _live_process_snapshot(
    identity: NativeProcess,
    directory: Path,
    proc_root: Path,
) -> NativeProcess:
    boot_id: str | None = None
    try:
        boot_id = (proc_root / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        cwd = (directory / "cwd").resolve(strict=True)
        command_line = _bounded_proc_read(directory / "cmdline")
    except _ProcReadBoundedError:
        return NativeProcess(
            identity.pid,
            NativeProcessState.BOUNDED,
            identity.uid,
            boot_id,
            identity.start_time,
            identity.comm,
        )
    except FileNotFoundError:
        return replace(identity, state=NativeProcessState.UNKNOWN)
    except (OSError, UnicodeDecodeError):
        return NativeProcess(
            identity.pid,
            NativeProcessState.UNKNOWN,
            identity.uid,
            boot_id,
            identity.start_time,
            identity.comm,
        )
    return NativeProcess(
        identity.pid,
        NativeProcessState.LIVE,
        identity.uid,
        boot_id,
        identity.start_time,
        identity.comm,
        cwd,
        command_line,
    )


def current_user_id() -> int:
    """Expose the process boundary's user identity to its callers."""
    return os.getuid()


def scan_native_processes(executable: str, proc_root: Path = Path("/proc")) -> NativeProcessScan:
    """Boundedly inspect same-user processes whose kernel comm names one native CLI."""
    observed: list[NativeProcess] = []
    complete = True
    inspected = 0
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            inspected += 1
            if inspected > _MAX_NATIVE_PROCESS_SCAN:
                complete = False
                break
            snapshot = _scan_native_process_candidate(entry, executable, proc_root)
            if snapshot is None or snapshot.state is NativeProcessState.ABSENT:
                continue
            if snapshot.state is not NativeProcessState.LIVE:
                complete = False
                continue
            observed.append(snapshot)
    except OSError:
        complete = False
    return NativeProcessScan(tuple(observed), complete)


def _scan_native_process_candidate(
    entry: Path, executable: str, proc_root: Path
) -> NativeProcess | None:
    identity = _process_identity(int(entry.name), proc_root)
    if identity.state is NativeProcessState.ABSENT or identity.comm != executable:
        return None
    if identity.uid is not None and identity.uid != os.getuid():
        return None
    if identity.state is NativeProcessState.ZOMBIE:
        return None
    if identity.state is not NativeProcessState.LIVE:
        return identity
    return _live_process_snapshot(identity, entry, proc_root)


class _ProcReadBoundedError(Exception):
    pass


def _bounded_proc_read(path: Path) -> bytes:
    with path.open("rb") as source:
        contents = source.read(_MAX_PROC_COMMAND_LINE_BYTES + 1)
    if len(contents) > _MAX_PROC_COMMAND_LINE_BYTES:
        raise _ProcReadBoundedError
    return contents


def _proc_stat(path: Path) -> tuple[str, str, int]:
    contents = path.read_text(encoding="utf-8")
    opened = contents.find("(")
    closed = contents.rfind(")")
    if opened <= 0 or closed <= opened:
        raise ValueError("malformed proc stat")
    fields = contents[closed + 2 :].split()
    if len(fields) <= _STAT_START_TIME_INDEX:
        raise ValueError("malformed proc stat")
    return fields[0], contents[opened + 1 : closed], int(fields[_STAT_START_TIME_INDEX])


def _proc_uid(path: Path) -> int:
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise ValueError("missing process uid")
