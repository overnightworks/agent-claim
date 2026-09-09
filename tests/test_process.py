from __future__ import annotations

from pathlib import Path

from agent_coordination import process


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


def test_process_observation_distinguishes_absent_malformed_zombie_and_racing_processes(
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
    assert process.inspect_native_process(42, tmp_path).state is process.NativeProcessState.ABSENT
    assert process.inspect_native_process(43, tmp_path).state is process.NativeProcessState.ABSENT
    assert process.inspect_native_process(44, tmp_path).state is process.NativeProcessState.UNKNOWN


def test_scan_ignores_absent_or_other_user_processes_and_marks_zombies_incomplete(
    tmp_path: Path,
) -> None:
    _write_process(tmp_path, 41, command=b"codex\0resume\0session-a\0", uid=999)
    _write_process(tmp_path, 42, command=b"codex\0resume\0session-a\0", state="Z")
    (tmp_path / "43").mkdir()

    scan = process.scan_native_processes("codex", tmp_path)

    assert scan.processes == ()
    assert scan.complete is False


def test_scan_marks_an_unreadable_proc_root_incomplete(tmp_path: Path) -> None:
    assert process.scan_native_processes("codex", tmp_path / "missing").complete is False


def _write_process(
    proc_root: Path,
    pid: int,
    *,
    command: bytes,
    state: str = "S",
    uid: int | None = None,
) -> None:
    (proc_root / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
    (proc_root / "sys/kernel/random/boot_id").write_text("boot\n")
    process_directory = proc_root / str(pid)
    process_directory.mkdir()
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
    (process_directory / "stat").write_text(f"{pid} (codex) " + " ".join(fields))
    owner = process.current_user_id() if uid is None else uid
    (process_directory / "status").write_text(f"Uid:\t{owner}\t0\t0\t0\n")
    (process_directory / "cmdline").write_bytes(command)
