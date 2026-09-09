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


def _write_process(proc_root: Path, pid: int, *, command: bytes) -> None:
    (proc_root / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
    (proc_root / "sys/kernel/random/boot_id").write_text("boot\n")
    process_directory = proc_root / str(pid)
    process_directory.mkdir()
    (process_directory / "cwd").symlink_to(proc_root)
    fields = [
        "S",
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
    (process_directory / "status").write_text(f"Uid:\t{process.current_user_id()}\t0\t0\t0\n")
    (process_directory / "cmdline").write_bytes(command)
