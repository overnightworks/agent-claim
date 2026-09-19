"""Behavior of `agent_coordination.process`: native `/proc` inspection, the
bounded subprocess boundary (`run_bounded`) it also owns, and the one git
launcher (`run_git`/`git_command`/`git_failure_detail`/
`git_failure_detail_from_stderr`, issue #372) `checkout` and `store` each call
rather than re-typing. The GitHub adapter's own thin
wrapper over that boundary (`github._bounded_command`) is covered in
`tests/test_github.py`; `checkout`'s and `store`'s own `ClaimError`
translations of a failed git launch are covered in their own test modules."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_coordination import process


def test_git_command_prefixes_the_directory_flag_only_when_given(tmp_path: Path) -> None:
    assert process.git_command(["status"]) == ["git", "status"]
    assert process.git_command(["status"], directory=tmp_path) == [
        "git",
        "-C",
        str(tmp_path),
        "status",
    ]


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (b"", b"fatal: not a git repository\n", "fatal: not a git repository"),
        (b"a stray warning on stdout\n", b"", "a stray warning on stdout"),
        (b"", b"", process.UNKNOWN_GIT_FAILURE),
    ],
)
def test_git_failure_detail_reads_stderr_then_stdout_then_the_fixed_sentence(
    stdout: bytes, stderr: bytes, expected: str
) -> None:
    result = process.CapturedResult(exit_status=1, stdout=stdout, stderr=stderr)

    assert process.git_failure_detail(result) == expected


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (b"", b"fatal: not a git repository\n", "fatal: not a git repository"),
        (b"a stray warning on stdout\n", b"", process.UNKNOWN_GIT_FAILURE),
        (b"", b"", process.UNKNOWN_GIT_FAILURE),
    ],
)
def test_git_failure_detail_from_stderr_never_reads_stdout(
    stdout: bytes, stderr: bytes, expected: str
) -> None:
    """Unlike `git_failure_detail`, a command whose stdout carries nothing a
    failure message should ever quote (`fetch`, `update-ref`, ...) must fall
    back straight to `UNKNOWN_GIT_FAILURE` rather than a stray stdout line
    (issue #372 R1)."""
    result = process.CapturedResult(exit_status=1, stdout=stdout, stderr=stderr)

    assert process.git_failure_detail_from_stderr(result) == expected


def test_run_git_launches_the_directory_scoped_git_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[list[str]] = []

    def fake_run_captured(command: list[str], **_kwargs: object) -> process.CapturedResult:
        observed.append(command)
        return process.CapturedResult(exit_status=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(process, "run_captured", fake_run_captured)

    result = process.run_git(["rev-parse", "HEAD"], directory=tmp_path)

    assert observed == [["git", "-C", str(tmp_path), "rev-parse", "HEAD"]]
    assert result.stdout == b"ok\n"


def test_inspect_native_process_reads_a_bounded_live_receipt_without_persisting_argv(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0")

    observed = process.inspect_native_process(41, tmp_path)

    assert observed.state is process.NativeProcessState.LIVE
    assert observed.start_time == 31
    assert observed.command_line == b"codex\0resume\0session-a\0"


def test_scan_marks_an_overlong_relevant_command_as_incomplete(tmp_path: Path) -> None:
    _write_process(tmp_path, 41, command=b"x" * (16 * 1024 + 1))

    scan = process.scan_native_processes("codex", tmp_path)

    assert scan.processes == ()
    assert scan.complete is False


def test_scan_refuses_to_treat_more_processes_than_its_bound_as_complete(tmp_path: Path) -> None:
    for pid in range(1, 1026):
        _write_process(tmp_path, pid, command=b"codex\0resume\0session-a\0")

    scan = process.scan_native_processes("codex", tmp_path)

    assert scan.complete is False


def test_process_observation_distinguishes_absent_malformed_zombie_and_unreadable_processes(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0", state="Z")
    _write_process(tmp_path, 42, command=b"codex\0resume\0session-a\0")
    (tmp_path / "42" / "cwd").unlink()
    (tmp_path / "43").mkdir()
    _write_process(tmp_path, 44, command=b"codex\0resume\0session-a\0")
    (tmp_path / "44" / "stat").write_text("not a stat record")

    assert process.inspect_native_process(0, tmp_path).state is process.NativeProcessState.ABSENT
    assert process.inspect_native_process(40, tmp_path).state is process.NativeProcessState.ABSENT
    assert process.inspect_native_process(41, tmp_path).state is process.NativeProcessState.ZOMBIE
    assert process.inspect_native_process(42, tmp_path).state is process.NativeProcessState.UNKNOWN
    assert process.inspect_native_process(43, tmp_path).state is process.NativeProcessState.ABSENT
    assert process.inspect_native_process(44, tmp_path).state is process.NativeProcessState.UNKNOWN


def test_a_fresh_observation_can_prove_an_unreadable_process_has_disappeared(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0", create_cwd=False)

    assert process.inspect_native_process(41, tmp_path).state is process.NativeProcessState.UNKNOWN
    (tmp_path / "41" / "stat").unlink()

    assert process.inspect_native_process(41, tmp_path).state is process.NativeProcessState.ABSENT


def test_scan_ignores_absent_other_user_and_zombie_processes(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0", uid=999)
    _write_process(tmp_path, 42, command=b"codex\0resume\0session-a\0", state="Z")
    (tmp_path / "43").mkdir()

    scan = process.scan_native_processes("codex", tmp_path)

    assert scan.processes == ()
    assert scan.complete is True


def test_scan_marks_an_unreadable_proc_root_incomplete(tmp_path: Path) -> None:
    assert process.scan_native_processes("codex", tmp_path / "missing").complete is False


def test_scan_marks_a_non_directory_proc_root_incomplete(tmp_path: Path) -> None:
    proc_root = tmp_path / "not-a-directory"
    proc_root.write_text("not a proc root")

    assert process.scan_native_processes("codex", proc_root).complete is False


def test_scan_never_reads_an_unrelated_process_command_or_working_directory(tmp_path: Path) -> None:
    _write_process(
        tmp_path,
        41,
        command=b"x" * (16 * 1024 + 1),
        comm="unrelated",
        create_cwd=False,
    )

    scan = process.scan_native_processes("codex", tmp_path)

    assert scan == process.NativeProcessScan((), True)


def test_scan_treats_a_relevant_process_with_unreadable_identity_as_incomplete(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0")
    (tmp_path / "41" / "status").write_text("Name:\tcodex\n")

    scan = process.scan_native_processes("codex", tmp_path)

    assert scan.processes == ()
    assert scan.complete is False


def test_process_observation_rejects_missing_uid_and_short_stat_records(tmp_path: Path) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0")
    _write_process(tmp_path, 42, command=b"codex\0resume\0session-a\0")
    _write_process(tmp_path, 43, command=b"codex\0resume\0session-a\0")
    (tmp_path / "41" / "status").write_text("Name:\tcodex\n")
    (tmp_path / "42" / "stat").write_text("42 (codex) S\n")
    (tmp_path / "43" / "status").unlink()

    assert process.inspect_native_process(41, tmp_path).state is process.NativeProcessState.UNKNOWN
    assert process.inspect_native_process(42, tmp_path).state is process.NativeProcessState.UNKNOWN
    assert process.inspect_native_process(43, tmp_path).state is process.NativeProcessState.ABSENT


def test_process_observation_treats_an_unreadable_command_as_unknown(tmp_path: Path) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0")
    command_path = tmp_path / "41" / "cmdline"
    command_path.unlink()
    command_path.mkdir()

    assert process.inspect_native_process(41, tmp_path).state is process.NativeProcessState.UNKNOWN


def test_scan_ignores_a_process_that_disappears_while_its_details_are_read(
    monkeypatch, tmp_path: Path
) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0")
    monkeypatch.setattr(
        process,
        "_live_process_snapshot",
        lambda identity, directory, proc_root: process.NativeProcess(
            identity.pid, process.NativeProcessState.ABSENT
        ),
    )

    scan = process.scan_native_processes("codex", tmp_path)

    assert scan == process.NativeProcessScan((), True)


def _write_process(
    proc_root: Path,
    pid: int,
    *,
    command: bytes,
    state: str = "S",
    uid: int | None = None,
    comm: str = "codex",
    create_cwd: bool = True,
) -> None:
    (proc_root / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
    (proc_root / "sys/kernel/random/boot_id").write_text("boot\n")
    process_directory = proc_root / str(pid)
    process_directory.mkdir()
    if create_cwd:
        (process_directory / "cwd").symlink_to(proc_root)
    fields = [
        state,
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "31",
    ]
    (process_directory / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
    owner = process.current_user_id() if uid is None else uid
    (process_directory / "status").write_text(f"Uid:\t{owner}\t0\t0\t0\n")
    (process_directory / "cmdline").write_bytes(command)


class _FakeBoundedProcess:
    """A deterministic `subprocess.Popen`-shaped double: a real closed pipe for
    `stdout` (so the selector has a real, immediately-EOF file descriptor to
    register) plus scripted `poll`/`terminate`/`kill`/`wait`, so
    `process.run_bounded`'s stop-and-reap behavior is proven without spawning a
    child or depending on real OS scheduling.
    """

    def __init__(self, *, already_exited: bool = False, ignores_terminate: bool = False) -> None:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb")
        self.stdin = None
        self.stderr = None
        self._ignores_terminate = ignores_terminate
        self.events: list[str] = []
        self._exited = already_exited

    def poll(self) -> int | None:
        return 0 if self._exited else None

    def terminate(self) -> None:
        self.events.append("terminate")
        if not self._ignores_terminate:
            self._exited = True

    def kill(self) -> None:
        self.events.append("kill")
        self._exited = True

    def wait(self, timeout: float = 0.0) -> int:
        if not self._exited:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return 0


def test_run_bounded_times_out_even_when_the_child_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline already passed by the time I/O is awaited must fail loud even
    when the child happened to finish first -- stopping an already-exited
    process is then a safe no-op, never a second signal or a raised error."""
    fake_process = _FakeBoundedProcess(already_exited=True)
    calls = {"n": 0}

    def already_past_deadline() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1_000_000.0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(process.time, "monotonic", already_past_deadline)

    with pytest.raises(process.ProcessTimedOutError):
        process.run_bounded(["fake"], timeout=5.0)

    assert fake_process.events == []
    assert fake_process.stdout.closed is True
    assert fake_process.poll() is not None


def test_run_bounded_kills_a_child_that_ignores_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that survives `terminate()` (ignoring the request to stop) must
    be `kill()`ed -- proven with a deterministic process fake controlling
    `poll`/`terminate`/`kill`/`wait`, not a real child ignoring a real SIGTERM."""
    fake_process = _FakeBoundedProcess(ignores_terminate=True)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(process.time, "monotonic", lambda: 0.0)

    with pytest.raises(process.ProcessTimedOutError):
        process.run_bounded(["fake"], timeout=-1.0)

    assert fake_process.events == ["terminate", "kill"]
    assert fake_process.stdout.closed is True
    assert fake_process.poll() is not None


def test_run_bounded_treats_a_broken_input_pipe_as_fully_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stdin write that raises `BrokenPipeError` -- the child closed its read
    end without consuming the input -- must not fail the whole exchange: the
    write is treated as fully sent and output collection continues."""

    def broken_write(_file_descriptor: int, data: bytes) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(process.os, "write", broken_write)

    observed = process.run_bounded(
        [sys.executable, "-c", "print('done')"],
        input_data=b"unread input",
    )
    assert observed.output == b"done\n"


def test_run_bounded_reaps_the_child_when_the_selector_fails_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selector that cannot close itself on the way out must not mask the
    command's own result -- reaping swallows that failure."""
    real_selector_class = process.selectors.DefaultSelector

    class CloseFailingSelector:
        def __init__(self) -> None:
            self._inner = real_selector_class()

        def register(self, fileobj, events, data=None):
            return self._inner.register(fileobj, events, data)

        def unregister(self, fileobj):
            return self._inner.unregister(fileobj)

        def get_map(self):
            return self._inner.get_map()

        def select(self, timeout=None):
            return self._inner.select(timeout)

        def close(self) -> None:
            raise OSError(5, "close failed")

    monkeypatch.setattr(process.selectors, "DefaultSelector", CloseFailingSelector)

    observed = process.run_bounded([sys.executable, "-c", "print('ok')"])
    assert observed.output == b"ok\n"
