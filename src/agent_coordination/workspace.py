"""Strict local workspace registration and one-head provider recovery lifecycle."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import protocol, providers, terminal

_CONFIG_VERSION = 3
_PROJECT_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_LIVE_PROJECT_FIELDS = frozenset(
    {"path", "session_id", "agent", "model", "provider", "external_process"}
)
_LOGIN_OWNER = "agent-coordination/login-v1"
_LOGIN_DESKTOP_NAME = "aco-workspace.desktop"
_LOGIN_ATTEMPT_NAME = "login-attempt.json"
_LOGIN_ENTRY_KEYS = frozenset({"Type", "Name", "Exec", "X-Aco-Owner"})
_DESKTOP_RESERVED_CHARACTERS = frozenset(" \t\n\"'\\><~|&;$*?#()`")
_ASCII_CONTROL_LIMIT = 32
_ASCII_DELETE = 127
_LOGIN_EXEC_ARGUMENTS = ("-I", "-m", "agent_coordination.cli", "_run-at-login")
_LOGIN_WORKSPACE_FAILURE = "workspace failure"
_LOGIN_MALFORMED_RECORD = "login attempt record is malformed"
_LOGIN_MALFORMED_LAUNCHER = "login launcher is malformed"


class WorkspaceError(protocol.ClaimError):
    pass


class RunState(StrEnum):
    STARTED = "started"
    REUSED = "reused"
    REATTACHED = "reattached"
    PENDING = "viewer pending"
    RETRIED = "retried"
    EXTERNAL = "external live"
    UNKNOWN = "ownership unknown"
    FAILED = "failed"


class LoginLauncherState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    STALE = "stale"
    CONFLICT = "conflict"


class LoginConfigurationState(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    MALFORMED = "malformed"
    EMPTY = "empty"


@dataclass(frozen=True)
class WorkspaceProject:
    key: str
    directory: Path
    session_id: str
    agent: str
    model: str | None = None
    provider: providers.Provider = providers.Provider.CODEX
    external_process: ExternalProcessReceipt | None = None


@dataclass(frozen=True)
class WorkspaceRegistration:
    key: str
    directory: Path
    session_id: str
    agent: str
    model: str | None = None
    provider: providers.Provider = providers.Provider.CODEX
    live_pid: int | None = None


@dataclass(frozen=True)
class ExternalProcessReceipt:
    boot_id: str
    pid: int
    start_time: int


@dataclass(frozen=True)
class WorkspaceConfig:
    projects: dict[str, WorkspaceProject]


@dataclass(frozen=True)
class RunOutcome:
    project: str
    state: RunState
    detail: str = ""


@dataclass(frozen=True)
class LoginAttempt:
    attempt_id: str
    started_at: str
    state: str
    outcomes: tuple[tuple[str, RunState], ...] = ()
    failure: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class LoginRunResult:
    attempt: LoginAttempt
    exit_status: int


def _config_root(environment: Mapping[str, str], home: Path | None = None) -> Path:
    """The one `~/.config/aco/` directory every local configuration file
    (the workspace registration, the board token) lives under -- shared so
    a second file never resolves its own, diverging root."""
    configured = environment.get("XDG_CONFIG_HOME")
    return (Path(configured) if configured else (home or Path.home()) / ".config") / "aco"


def default_config_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    return _config_root(environment, home) / "workspace.toml"


BOARD_TOKEN_BYTES = 32
"""`secrets.token_urlsafe`'s own byte count for `board --serve`'s persistent
loopback token (issue #388) -- matches the per-start token's prior entropy
(#280), only now minted once and read back rather than minted per start."""


_BOARD_DIRECTORY_NAME_COMPONENTS = 2
_BOARD_DIRECTORY_HASH_CHARACTERS = 8
_UNREADABLE_IN_A_BOARD_DIRECTORY = re.compile(r"[^A-Za-z0-9_-]+")


def _board_directory_name(repository: str) -> str:
    """One directory name per board identity: the last two components of
    `repository` -- `owner/repo` on a forge, the checkout's own parent and
    directory where the canonical remote is a local path -- made readable,
    plus a short digest of the whole identity. The digest is what keeps two
    identities apart (issue #431): the readable part alone collides
    whenever a name carries a separator character, and a shared directory
    is exactly the bug this path exists to remove."""
    tail = repository.strip("/").split("/")[-_BOARD_DIRECTORY_NAME_COMPONENTS:]
    readable = _UNREADABLE_IN_A_BOARD_DIRECTORY.sub("-", "-".join(tail))
    digest = hashlib.sha256(repository.encode("utf-8")).hexdigest()
    return f"{readable}-{digest[:_BOARD_DIRECTORY_HASH_CHARACTERS]}"


@dataclass(frozen=True)
class BoardTokenLocation:
    """Where one repository's served board keeps its token, and every
    directory above it this tool creates itself, outermost first (issue
    #431). The token now lives one directory per board, so the privacy
    check that used to cover a single `~/.config/aco` covers each of those
    levels: a symlink swapped in at any one of them would otherwise hand
    the token to whoever owns its target."""

    file: Path
    directories: tuple[Path, ...]


def default_board_token_location(
    repository: str, environment: Mapping[str, str], home: Path | None = None
) -> BoardTokenLocation:
    """`board --serve`'s persistent token file for one repository (issues
    #388, #431): the same `~/.config/aco/` root `default_config_path`
    already owns, never a second configuration source, but one directory
    per board identity under it -- `repository` names whose board this is,
    so a token minted for one repository never opens another repository's
    served board, and the printed URL still stays stable across restarts
    and reinstalls."""
    root = _config_root(environment, home)
    boards = root / "boards"
    board = boards / _board_directory_name(repository)
    return BoardTokenLocation(file=board / "token", directories=(root, boards, board))


def board_token(location: BoardTokenLocation, *, mint_new: bool = False) -> str:
    """`location`'s persistent token: read back when it already exists and a
    fresh one was not requested, minted (`secrets.token_urlsafe`, written
    0600) otherwise -- `mint_new` is `--new-token`'s own request to replace
    it. An existing file whose mode has drifted from 0600, is not a regular
    file this user owns, or is a symlink refuses by name rather than being
    trusted: the token is the one secret this command holds, and it is
    never logged anywhere but the one printed URL line. Every directory
    `location` names is checked the same way on every call, not only when
    this call creates it, so an operator-shared `~/.config/aco` left group-
    or world-writable is never silently trusted either."""
    for directory in location.directories:
        _ensure_board_token_directory(directory)
    if mint_new:
        token = secrets.token_urlsafe(BOARD_TOKEN_BYTES)
        _mint_board_token(location.file, token)
        return token
    return _first_board_token(location.file)


class _BoardTokenNotFoundError(Exception):
    """Internal marker (issue #388, round 4): `path` has nothing minted at
    it yet -- `_read_board_token`'s own signal, raised only on the open
    call's `FileNotFoundError`, that `_first_board_token` catches to know a
    mint is needed instead of a refusal. Never a `WorkspaceError`: it never
    reaches a caller outside this module."""


def _write_temporary_token_file(directory: Path, name: str, content: bytes) -> str:
    """A private (0600), fsynced temporary file in `directory` holding
    `content`, ready to be published at `name`'s own path by `os.link` or
    `os.replace` -- shared by both mint paths below so a token is only ever
    visible at its final name once fully written, never as an empty or
    partial file there. A write, flush, fsync, or chmod failure after
    `mkstemp` removes that temporary file (best effort) before re-raising,
    so a failed mint never leaves a `.token.*` file behind."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, _PRIVATE_FILE_MODE)
    except OSError:
        _discard_temporary_token_file(temporary_name)
        raise
    return temporary_name


def _discard_temporary_token_file(temporary_name: str) -> None:
    """Best-effort cleanup of a mint's own now-unneeded temporary file,
    called only after the mint's real outcome -- a token to return, the
    winner's file to read back, or a `WorkspaceError` already decided from
    the failure that preceded this call -- is settled. An unlink failure
    here, `ENOENT` or anything else, is swallowed rather than raised: the
    file is disposable litter once that outcome exists, so surfacing a
    second, unrelated `OSError` here would only replace a successful
    result or the named refusal with a worse, un-named one."""
    with suppress(OSError):
        os.unlink(temporary_name)


def _mint_board_token(path: Path, token: str) -> None:
    """`--new-token`'s own mint: publishes by `os.replace`, an ordinary
    atomic rename that always succeeds against whatever was at `path`
    before -- there is only ever one `--new-token` writer, so none of
    `_first_board_token`'s own first-writer race below applies here. Any
    failure -- an owner-unwritable directory, a full disk -- refuses naming
    the token path rather than raising a raw `OSError` or reaching
    `_atomic_write`'s unrelated "login recovery state" wording, since this
    path never touches that state; a `replace` failure also removes the
    temporary file it could not publish."""
    try:
        temporary_name = _write_temporary_token_file(
            path.parent, path.name, (token + "\n").encode()
        )
    except OSError as error:
        raise WorkspaceError(_invalid_board_token_message(path)) from error
    try:
        os.replace(temporary_name, path)
    except OSError as error:
        _discard_temporary_token_file(temporary_name)
        raise WorkspaceError(_invalid_board_token_message(path)) from error


def _first_board_token(path: Path) -> str:
    """The token at `path`: read back when a start finds one already
    minted -- a read needs no write access to `path`'s own directory, so
    BOARD-32's "minted only when missing" holds even when that directory is
    owner-unwritable -- minting only when the open itself reports there is
    nothing there yet (`_BoardTokenNotFoundError`, raised only on `ENOENT`; any
    other read failure is `_read_board_token`'s own refusal to raise, not a
    "go ahead and mint" signal). The mint writes a private, fsynced
    temporary file in the same directory and publishes it with `os.link`
    rather than `O_CREAT | O_EXCL` on the final path itself: `link` either
    creates `path` pointing at the same, fully-written inode, or fails
    `EEXIST` because another process's mint already won -- there is never a
    window where `path` exists but is still empty, the way opening the
    final name directly for writing would leave one. The loser reads the
    winner's file back instead of writing a second, diverging token; an
    owner-unwritable directory refuses naming the token path the same way
    `_mint_board_token` does."""
    try:
        return _read_board_token(path)
    except _BoardTokenNotFoundError:
        pass
    token = secrets.token_urlsafe(BOARD_TOKEN_BYTES)
    try:
        temporary_name = _write_temporary_token_file(
            path.parent, path.name, (token + "\n").encode()
        )
    except OSError as error:
        raise WorkspaceError(_invalid_board_token_message(path)) from error
    try:
        os.link(temporary_name, path)
    except FileExistsError:
        return _read_board_token(path)
    except OSError as error:
        raise WorkspaceError(_invalid_board_token_message(path)) from error
    finally:
        _discard_temporary_token_file(temporary_name)
    return token


_PRIVATE_FILE_MODE = 0o600
_BOARD_TOKEN_CONTENT_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\n?")
"""Exactly one `secrets.token_urlsafe(32)` value (43 url-safe characters),
with or without the trailing newline `board_token` itself always writes --
`_first_board_token`'s own atomic mint above, or a hand-edited file, are the
only ways this file's content can differ, and either is refused rather than
trusted into an authorization comparison."""


def _invalid_board_token_message(path: Path) -> str:
    return f"board token at {path} is not a valid token; pass --new-token"


def _read_board_token(path: Path) -> str:
    """`path`'s token, opened `O_NOFOLLOW | O_NONBLOCK` and validated on the
    open descriptor's own `fstat` -- a regular file, owned by this user,
    mode 0600 -- rather than on a separate `lstat`/`read_text` pair a
    concurrent replace or a symlink swap could race between. `O_NONBLOCK`
    keeps a FIFO left at this path from blocking startup on a writer that
    may never arrive; the `S_ISREG` check below refuses it by name the
    moment `fstat` reports it, before any read is attempted. Content is
    read as bytes and decoded only after every `fstat` check passes, so
    invalid UTF-8 refuses the same named way an `OSError` on open or read
    does, rather than raising a bare `UnicodeDecodeError`. The open's own
    `FileNotFoundError` is the one exception raised as `_BoardTokenNotFoundError`
    instead: `_first_board_token`'s own "nothing minted yet" signal, never
    a refusal a caller outside this module would see."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise _BoardTokenNotFoundError from error
    except OSError as error:
        raise WorkspaceError(_invalid_board_token_message(path)) from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise WorkspaceError(_invalid_board_token_message(path))
            mode = stat.S_IMODE(metadata.st_mode)
            if mode != _PRIVATE_FILE_MODE:
                raise WorkspaceError(
                    f"board token file {path} must be private (mode 0600, found {mode:04o})"
                )
            raw = handle.read()
    except OSError as error:
        raise WorkspaceError(_invalid_board_token_message(path)) from error
    try:
        contents = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceError(_invalid_board_token_message(path)) from error
    if not _BOARD_TOKEN_CONTENT_PATTERN.fullmatch(contents):
        raise WorkspaceError(_invalid_board_token_message(path))
    return contents.rstrip("\n")


def _ensure_board_token_directory(path: Path) -> None:
    """`board_token`'s own parent-directory guard (issue #388): owned by
    this user, not a symlink, and carrying neither the group nor the other
    WRITE bit -- naming the path and the actual mode in its refusal, unlike
    `_ensure_private_directory`'s own symlink/owner/mode shape for the login
    launcher below. Only the WRITE bit is checked, so an ordinary `umask
    022` directory (`0755`, merely group/world-readable) passes; a mint
    still refuses by name if that mode leaves the directory itself
    unwritable to its own owner. The token is the one secret this directory
    holds, so an existing, wrongly-shared `~/.config/aco` is checked exactly
    like a freshly created one; `Path.mkdir(exist_ok=True)` never revisits
    an existing directory's mode on its own."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise WorkspaceError(f"cannot create board token directory {path}") from error
    mode = stat.S_IMODE(metadata.st_mode)
    unsafe = (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or mode & 0o022
    )
    if unsafe:
        raise WorkspaceError(
            f"board token directory {path} must be private and owned by this user "
            f"(found mode {mode:04o})"
        )


def login_desktop_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    configured = environment.get("XDG_CONFIG_HOME")
    root = Path(configured) if configured else (home or _environment_home(environment)) / ".config"
    return root / "autostart" / _LOGIN_DESKTOP_NAME


def login_attempt_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    configured = environment.get("XDG_STATE_HOME")
    root = (
        Path(configured)
        if configured
        else (home or _environment_home(environment)) / ".local" / "state"
    )
    return root / "aco" / _LOGIN_ATTEMPT_NAME


def enable_login(config_path: Path, environment: Mapping[str, str], executable: Path) -> bool:
    """Install the one owned desktop entry after strict whole-workspace validation."""
    config = load_config(config_path)
    if not config.projects:
        raise WorkspaceError("workspace configuration has no registered projects")
    executable = _login_executable(executable)
    desktop_path = login_desktop_path(environment)
    _ensure_private_directory(desktop_path.parent)
    existing = _owned_launcher_executable(desktop_path)
    if desktop_path.exists() or desktop_path.is_symlink():
        if existing is None:
            raise WorkspaceError("login launcher conflicts with an unowned filesystem object")
        if existing == executable:
            return False
    _atomic_write(desktop_path, _desktop_entry(executable).encode())
    return True


def disable_login(environment: Mapping[str, str]) -> bool:
    """Remove only the exact entry generated by this installed program."""
    desktop_path = login_desktop_path(environment)
    if not desktop_path.exists() and not desktop_path.is_symlink():
        return False
    if _owned_launcher_executable(desktop_path) is None:
        raise WorkspaceError("login launcher conflicts with an unowned filesystem object")
    try:
        desktop_path.unlink()
    except OSError as error:
        raise WorkspaceError("cannot remove login launcher") from error
    return True


def login_launcher_state(environment: Mapping[str, str], executable: Path) -> LoginLauncherState:
    desktop_path = login_desktop_path(environment)
    if not desktop_path.exists() and not desktop_path.is_symlink():
        return LoginLauncherState.DISABLED
    owned_executable = _owned_launcher_executable(desktop_path)
    if owned_executable is None:
        return LoginLauncherState.CONFLICT
    try:
        current_executable = _login_executable(executable)
    except WorkspaceError:
        return LoginLauncherState.STALE
    return (
        LoginLauncherState.ENABLED
        if owned_executable == current_executable
        else LoginLauncherState.STALE
    )


def login_configuration_state(config_path: Path) -> LoginConfigurationState:
    try:
        config = load_config(config_path)
    except WorkspaceError:
        if not config_path.exists():
            return LoginConfigurationState.MISSING
        return LoginConfigurationState.MALFORMED
    return LoginConfigurationState.VALID if config.projects else LoginConfigurationState.EMPTY


def run_login_recovery(
    config_path: Path,
    state_path: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    new_attempt_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> LoginRunResult:
    """Serialize one desired-workspace run and preserve only bounded public state."""
    _ensure_private_directory(state_path.parent)
    started = _login_time(now())
    attempt = LoginAttempt(new_attempt_id(), started, "running")
    with _locked(state_path.with_suffix(".lock")):
        _write_login_attempt(state_path, attempt)
        try:
            outcomes = run_projects(config_path)
        except (WorkspaceError, terminal.TerminalError):
            completed = LoginAttempt(
                attempt.attempt_id,
                attempt.started_at,
                "completed",
                failure=_LOGIN_WORKSPACE_FAILURE,
                completed_at=_login_time(now()),
            )
            _write_login_attempt(state_path, completed)
            return LoginRunResult(completed, 2)
        if not outcomes:
            completed = LoginAttempt(
                attempt.attempt_id,
                attempt.started_at,
                "completed",
                failure=_LOGIN_WORKSPACE_FAILURE,
                completed_at=_login_time(now()),
            )
            _write_login_attempt(state_path, completed)
            return LoginRunResult(completed, 2)
        completed = LoginAttempt(
            attempt.attempt_id,
            attempt.started_at,
            "completed",
            tuple((outcome.project, outcome.state) for outcome in outcomes),
            completed_at=_login_time(now()),
        )
        _write_login_attempt(state_path, completed)
    return LoginRunResult(
        completed,
        2
        if any(state in {RunState.FAILED, RunState.UNKNOWN} for _, state in completed.outcomes)
        else 0,
    )


def load_login_attempt(state_path: Path) -> LoginAttempt:
    try:
        raw = json.loads(state_path.read_text())
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    return _login_attempt_from_record(raw)


def _login_attempt_from_record(raw: object) -> LoginAttempt:
    if not isinstance(raw, dict):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    required = {"version", "attempt_id", "started_at", "state", "outcomes"}
    allowed = required | {"failure", "completed_at"}
    if set(raw) - allowed or not required <= set(raw) or raw.get("version") != 1:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    attempt_id = raw["attempt_id"]
    started_at = raw["started_at"]
    state = raw["state"]
    _login_attempt_identifier(attempt_id)
    started = _login_timestamp(started_at)
    if state not in {"running", "completed"}:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    parsed_outcomes = _login_outcomes(raw["outcomes"])
    failure, completed_at = _login_completion(raw, state, parsed_outcomes, started)
    return LoginAttempt(attempt_id, started_at, state, parsed_outcomes, failure, completed_at)


def _login_outcomes(raw_outcomes: object) -> tuple[tuple[str, RunState], ...]:
    if not isinstance(raw_outcomes, list):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    parsed_outcomes: list[tuple[str, RunState]] = []
    for outcome in raw_outcomes:
        if not isinstance(outcome, dict) or set(outcome) != {"project", "outcome"}:
            raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
        project, outcome_state = outcome["project"], outcome["outcome"]
        if not isinstance(project, str) or not _PROJECT_KEY_PATTERN.fullmatch(project):
            raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
        try:
            parsed_outcomes.append((project, RunState(outcome_state)))
        except (TypeError, ValueError) as error:
            raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    return tuple(parsed_outcomes)


def _login_completion(
    raw: Mapping[str, object],
    state: object,
    outcomes: tuple[tuple[str, RunState], ...],
    started: datetime,
) -> tuple[str | None, str | None]:
    failure = _login_failure(raw.get("failure"))
    completed_at = _login_completed_at(raw.get("completed_at"))
    completed = None if completed_at is None else _login_timestamp(completed_at)
    if state == "running" and (outcomes or failure is not None or completed_at is not None):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if state == "completed" and completed_at is None:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if completed is not None and completed < started:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if failure is not None and outcomes:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if state == "completed" and failure is None and not outcomes:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    return failure, completed_at


def _login_failure(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != _LOGIN_WORKSPACE_FAILURE:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    return value


def _login_completed_at(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    _login_timestamp(value)
    return value


def _login_attempt_identifier(value: object) -> None:
    if not isinstance(value, str):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    try:
        identifier = uuid.UUID(value)
    except ValueError as error:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    if str(identifier) != value or identifier.version != terminal.PENDING_TOKEN_VERSION:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)


def _login_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    if parsed.tzinfo is None:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    return parsed


def register_project(handoff: WorkspaceRegistration, config_path: Path) -> bool:
    """Store an explicit stopped or process-validated live handoff."""
    candidate = _project(handoff)
    if handoff.live_pid is not None:
        receipt = terminal.register_live_process(
            candidate.provider, candidate.session_id, candidate.directory, handoff.live_pid
        )
        candidate = replace(
            candidate,
            external_process=ExternalProcessReceipt(
                receipt.boot_id, receipt.pid, receipt.start_time
            ),
        )
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(config_path.with_suffix(".lock")):
        projects = {} if not config_path.exists() else dict(load_config(config_path).projects)
        existing = projects.get(candidate.key)
        if existing is not None:
            if existing == candidate:
                return False
            raise WorkspaceError(
                f"project {candidate.key!r} is already registered; "
                "native session and claim identity "
                "can only change through an explicit future handoff"
            )
        if any(project.directory == candidate.directory for project in projects.values()):
            raise WorkspaceError(f"directory {candidate.directory} is already registered")
        if any(
            (project.provider, project.session_id) == (candidate.provider, candidate.session_id)
            for project in projects.values()
        ):
            raise WorkspaceError(
                f"{candidate.provider.value} session {candidate.session_id} is already registered"
            )
        projects[candidate.key] = candidate
        _write_config(config_path, projects)
    return True


def load_config(config_path: Path) -> WorkspaceConfig:
    """Read the entire local mapping or reject it before any project starts."""
    try:
        contents = config_path.read_bytes()
    except FileNotFoundError as error:
        raise WorkspaceError(f"workspace configuration does not exist: {config_path}") from error
    except OSError as error:
        raise WorkspaceError(
            f"cannot read workspace configuration {config_path}: {error}"
        ) from error
    try:
        raw = tomllib.loads(contents.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise WorkspaceError(f"invalid workspace configuration {config_path}: {error}") from error
    version = raw.get("version")
    if (
        set(raw) != {"version", "projects"}
        or type(version) is not int
        or version != _CONFIG_VERSION
    ):
        raise WorkspaceError(
            f"workspace configuration {config_path} must contain only "
            f"version = {_CONFIG_VERSION} and projects; found version {version!r}"
        )
    raw_projects = raw["projects"]
    if not isinstance(raw_projects, dict):
        raise WorkspaceError("workspace configuration projects must be a mapping")
    projects: dict[str, WorkspaceProject] = {}
    for key, record in raw_projects.items():
        project = _project_from_config_record(key, record)
        projects[project.key] = project
    _validate_unique_projects(projects)
    return WorkspaceConfig(projects)


def run_projects(
    config_path: Path,
    project_key: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    runtime_directory: Path | None = None,
    terminal_factory: Callable[[Path], terminal.TerminalController] = terminal.TmuxTerminal,
) -> tuple[RunOutcome, ...]:
    """Run selected projects in configuration order after whole-config validation."""
    config = load_config(config_path)
    if project_key is not None and project_key not in config.projects:
        raise WorkspaceError(f"project {project_key!r} is not registered")
    selected = (
        (config.projects[project_key],)
        if project_key is not None
        else tuple(config.projects.values())
    )
    runtime = _runtime_directory(runtime_directory, environment or os.environ)
    controller = terminal_factory(runtime / "tmux.sock")
    child_environment = environment or os.environ
    outcomes: list[RunOutcome] = []
    with _locked(runtime / "workspace.lock"):
        for project in selected:
            try:
                outcomes.append(_run_project(controller, project, child_environment))
            except terminal.TerminalError as error:
                outcomes.append(RunOutcome(project.key, RunState.FAILED, str(error)))
    return tuple(outcomes)


def _run_project(
    controller: terminal.TerminalController,
    project: WorkspaceProject,
    environment: Mapping[str, str],
) -> RunOutcome:
    target = controller.inspect(project.key)
    launch = terminal.Launch(
        providers.resume_command(
            project.provider, project.session_id, project.directory, project.model
        ),
        providers.project_environment(environment, project.agent),
        providers.session_identity_environment_names(),
        project.provider,
    )
    if target.state is terminal.TargetState.ABSENT:
        return _launch_if_unowned(controller, project, launch, RunState.STARTED, environment)
    if not terminal.target_matches(
        {
            "@aco_project": target.project or "",
            "@aco_provider": target.provider.value if target.provider is not None else "",
            "@aco_session_id": target.session_id or "",
        },
        project.key,
        project.provider,
        project.session_id,
    ):
        raise terminal.TerminalError(
            f"tmux target {terminal.target_name(project.key)!r} has foreign metadata"
        )
    if target.state is terminal.TargetState.ATTACHED:
        _consume_attached_viewer_failure(controller, project, target)
        return RunOutcome(project.key, RunState.REUSED)
    if _is_failed_viewer_attempt(target):
        return _consume_failed_viewer_attempt(controller, project, target)
    if target.state is terminal.TargetState.EXITED:
        return _launch_if_unowned(controller, project, launch, RunState.RETRIED, environment)
    if target.viewer_attempt is not None:
        return RunOutcome(
            project.key, RunState.PENDING, "waiting for the previously launched console"
        )
    return _attach_or_pending(controller, project, RunState.REATTACHED, environment)


def _external_ownership(
    project: WorkspaceProject,
) -> RunOutcome | None:
    receipt = (
        None
        if project.external_process is None
        else terminal.ExternalProcessReceipt(
            project.external_process.boot_id,
            project.external_process.pid,
            project.external_process.start_time,
        )
    )
    ownership = terminal.external_ownership(
        project.provider, project.session_id, project.directory, receipt
    )
    if ownership.state is terminal.ExternalOwnershipState.LIVE:
        return RunOutcome(project.key, RunState.EXTERNAL)
    if ownership.state is terminal.ExternalOwnershipState.UNKNOWN:
        return RunOutcome(project.key, RunState.UNKNOWN)
    return None


def _launch_if_unowned(
    controller: terminal.TerminalController,
    project: WorkspaceProject,
    launch: terminal.Launch,
    state: RunState,
    environment: Mapping[str, str],
) -> RunOutcome:
    ownership = _external_ownership(project) if project.external_process is not None else None
    if ownership is not None:
        return ownership
    if state is RunState.STARTED:
        controller.create(project.key, project.session_id, project.directory, launch)
    else:
        controller.retry(project.key, project.directory, launch)
    return _attach_or_pending(controller, project, state, environment)


def _attach_or_pending(
    controller: terminal.TerminalController,
    project: WorkspaceProject,
    state: RunState,
    environment: Mapping[str, str],
) -> RunOutcome:
    target = controller.inspect(project.key)
    existing = _existing_viewer_outcome(
        controller, project, target, state, "waiting for the previously launched console"
    )
    if existing is not None:
        return existing
    controller.open_viewer(project.key, environment)
    observed = controller.inspect(project.key)
    existing = _existing_viewer_outcome(
        controller, project, observed, state, "console launch is pending"
    )
    if existing is not None:
        return existing
    return RunOutcome(project.key, RunState.FAILED, "project console is not attached")


def _existing_viewer_outcome(
    controller: terminal.TerminalController,
    project: WorkspaceProject,
    target: terminal.Target,
    state: RunState,
    pending_detail: str,
) -> RunOutcome | None:
    if target.state is terminal.TargetState.ATTACHED:
        outcome_state = (
            RunState.REUSED
            if _consume_attached_viewer_failure(controller, project, target)
            else state
        )
        return RunOutcome(project.key, outcome_state)
    if _is_failed_viewer_attempt(target):
        return _consume_failed_viewer_attempt(controller, project, target)
    if target.viewer_attempt is not None:
        return RunOutcome(project.key, RunState.PENDING, pending_detail)
    return None


def _is_failed_viewer_attempt(target: terminal.Target) -> bool:
    return (
        target.viewer_attempt is not None
        and target.viewer_attempt.state is terminal.ViewerAttemptState.FAILED
    )


def _consume_attached_viewer_failure(
    controller: terminal.TerminalController, project: WorkspaceProject, target: terminal.Target
) -> bool:
    if _is_failed_viewer_attempt(target):
        assert target.viewer_attempt is not None
        controller.consume_viewer_failure(project.key, target.viewer_attempt.token)
        return True
    return False


def _consume_failed_viewer_attempt(
    controller: terminal.TerminalController, project: WorkspaceProject, target: terminal.Target
) -> RunOutcome:
    assert target.viewer_attempt is not None
    controller.consume_viewer_failure(project.key, target.viewer_attempt.token)
    return RunOutcome(project.key, RunState.FAILED, "project console failed to attach")


def _project(handoff: WorkspaceRegistration) -> WorkspaceProject:
    key = handoff.key
    directory = handoff.directory
    session_id = handoff.session_id
    agent = handoff.agent
    model = handoff.model
    provider = _provider(handoff.provider)
    if _PROJECT_KEY_PATTERN.fullmatch(key) is None:
        raise WorkspaceError("project key must use letters, numbers, underscores, or hyphens")
    path = Path(directory).expanduser()
    try:
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise WorkspaceError(f"project {key!r} directory does not exist: {path}") from error
    if not canonical.is_dir():
        raise WorkspaceError(f"project {key!r} directory is not a directory: {canonical}")
    try:
        parsed_uuid = uuid.UUID(session_id)
    except (AttributeError, ValueError) as error:
        raise WorkspaceError(f"project {key!r} session_id must be an exact UUID") from error
    if str(parsed_uuid) != session_id:
        raise WorkspaceError(f"project {key!r} session_id must be an exact UUID")
    if not agent or not agent.strip():
        raise WorkspaceError(f"project {key!r} agent must not be empty")
    _validate_launch_identifier(agent, f"project {key!r} agent")
    if model is not None and (not model or not model.strip()):
        raise WorkspaceError(f"project {key!r} model must not be empty")
    if model is not None:
        _validate_launch_identifier(model, f"project {key!r} model")
    if handoff.live_pid is not None and (
        type(handoff.live_pid) is not int or handoff.live_pid <= 0
    ):
        raise WorkspaceError(f"project {key!r} live_pid must be a positive integer")
    return WorkspaceProject(key, canonical, session_id, agent, model, provider)


def _project_from_config_record(key: object, record: object) -> WorkspaceProject:
    if not isinstance(key, str) or not isinstance(record, dict):
        raise WorkspaceError("workspace projects must use project keys and table records")
    required = {"path", "session_id", "agent", "provider"}
    if set(record) - _LIVE_PROJECT_FIELDS or not required <= set(record):
        raise WorkspaceError(f"project {key!r} has unsupported or missing fields")
    directory = _canonical_config_directory(_string(record["path"], f"project {key!r} path"))
    session_id = _string(record["session_id"], f"project {key!r} session_id")
    agent = _string(record["agent"], f"project {key!r} agent")
    model = _optional_string(record.get("model"), f"project {key!r} model")
    provider = _provider(record["provider"], f"project {key!r} provider")
    project = _project(
        WorkspaceRegistration(
            key,
            directory,
            session_id,
            agent,
            model,
            provider=provider,
        )
    )
    receipt = _external_process_from_record(record.get("external_process"), key)
    if receipt is None:
        return project
    return replace(project, external_process=receipt)


def _external_process_from_record(value: object, key: object) -> ExternalProcessReceipt | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"boot_id", "pid", "start_time"}:
        raise WorkspaceError(f"project {key!r} external process receipt is malformed")
    boot_id, pid, start_time = value["boot_id"], value["pid"], value["start_time"]
    if (
        not isinstance(boot_id, str)
        or not boot_id
        or type(pid) is not int
        or pid <= 0
        or type(start_time) is not int
        or start_time <= 0
    ):
        raise WorkspaceError(f"project {key!r} external process receipt is malformed")
    return ExternalProcessReceipt(boot_id, pid, start_time)


def _validate_unique_projects(projects: Mapping[str, WorkspaceProject]) -> None:
    directories = [project.directory for project in projects.values()]
    sessions = [(project.provider, project.session_id) for project in projects.values()]
    if len(directories) != len(set(directories)):
        raise WorkspaceError("workspace configuration contains duplicate canonical paths")
    if len(sessions) != len(set(sessions)):
        raise WorkspaceError("workspace configuration contains duplicate native provider UUIDs")


def _provider(value: providers.Provider | str, name: str = "provider") -> providers.Provider:
    try:
        return providers.Provider(value)
    except (TypeError, ValueError) as error:
        choices = ", ".join(provider.value for provider in providers.Provider)
        raise WorkspaceError(f"{name} must be one of {choices}") from error


def _canonical_config_directory(value: str) -> Path:
    configured = Path(value)
    if not configured.is_absolute():
        raise WorkspaceError("workspace project path must be an absolute canonical directory")
    try:
        canonical = configured.resolve(strict=True)
    except OSError as error:
        raise WorkspaceError(f"workspace project directory does not exist: {configured}") from error
    if configured != canonical:
        raise WorkspaceError("workspace project path must be a canonical directory")
    return canonical


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceError(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _validate_launch_identifier(value: str, name: str) -> None:
    if "\0" in value or "\r" in value or "\n" in value:
        raise WorkspaceError(f"{name} must not contain line breaks or NUL")


def _runtime_directory(runtime_directory: Path | None, environment: Mapping[str, str]) -> Path:
    candidate = runtime_directory or Path(environment.get("XDG_RUNTIME_DIR", ""))
    if not candidate.is_absolute():
        raise WorkspaceError("XDG_RUNTIME_DIR must be an absolute user-owned directory")
    try:
        metadata = candidate.stat()
    except OSError as error:
        raise WorkspaceError(f"XDG_RUNTIME_DIR is unavailable: {candidate}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WorkspaceError("XDG_RUNTIME_DIR must be a private directory owned by this user")
    root = candidate / "aco"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise WorkspaceError(
            "workspace runtime directory must be private and must not be a symlink"
        )
    return root


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _environment_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME")
    return Path(configured) if configured else Path.home()


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.stat()
    except OSError as error:
        raise WorkspaceError("cannot create private login directory") from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WorkspaceError("login directory must be private and owned by this user")


def _login_executable(executable: Path) -> Path:
    if not executable.is_absolute():
        raise WorkspaceError("login executable must be an absolute safe path")
    lexical = Path(os.path.abspath(executable))
    if (
        "=" in str(lexical)
        or "%" in str(lexical)
        or _unsafe_desktop_text(str(lexical))
        or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", lexical.name, flags=re.ASCII) is None
    ):
        raise WorkspaceError("login executable must be an absolute safe path")
    return lexical


def _desktop_entry(executable: Path) -> str:
    arguments = (str(executable), *_LOGIN_EXEC_ARGUMENTS)
    return "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Name=ACO workspace recovery",
            "Exec=" + " ".join(_desktop_argument(argument) for argument in arguments),
            f"X-Aco-Owner={_LOGIN_OWNER}",
            "",
        )
    )


def _desktop_argument(argument: str) -> str:
    if not argument or any(character in _DESKTOP_RESERVED_CHARACTERS for character in argument):
        quoted = "".join(
            f"\\{character}" if character in {"\\", '"', "`", "$"} else character
            for character in argument
        )
        return '"' + quoted.replace("\\", "\\\\") + '"'
    return argument


def _unsafe_desktop_text(value: str) -> bool:
    return any(
        ord(character) < _ASCII_CONTROL_LIMIT or ord(character) == _ASCII_DELETE
        for character in value
    )


def _owned_launcher_executable(path: Path) -> Path | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        return None
    try:
        return _launcher_executable(path.read_text())
    except (OSError, UnicodeDecodeError, WorkspaceError):
        return None


def _launcher_executable(contents: str) -> Path:
    if not contents.endswith("\n"):
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    lines = contents.splitlines()
    if not lines or lines[0] != "[Desktop Entry]":
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    values: dict[str, str] = {}
    for line in lines[1:]:
        if "=" not in line:
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        key, value = line.split("=", maxsplit=1)
        if key not in _LOGIN_ENTRY_KEYS or key in values:
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        values[key] = value
    if set(values) != _LOGIN_ENTRY_KEYS or values.get("Type") != "Application":
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    if values.get("Name") != "ACO workspace recovery" or values.get("X-Aco-Owner") != _LOGIN_OWNER:
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    arguments = _desktop_exec_arguments(values["Exec"])
    if len(arguments) != len(_LOGIN_EXEC_ARGUMENTS) + 1 or arguments[1:] != _LOGIN_EXEC_ARGUMENTS:
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    return _login_executable(Path(arguments[0]))


def _desktop_exec_arguments(value: str) -> tuple[str, ...]:
    unescaped = _desktop_string_unescape(value)
    arguments: list[str] = []
    index = 0
    while index < len(unescaped):
        if unescaped[index] == " ":
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        argument, index = (
            _quoted_desktop_argument(unescaped, index + 1)
            if unescaped[index] == '"'
            else _unquoted_desktop_argument(unescaped, index)
        )
        arguments.append(argument)
        if index < len(unescaped):
            index += 1
    return tuple(arguments)


def _quoted_desktop_argument(value: str, index: int) -> tuple[str, int]:
    characters: list[str] = []
    while index < len(value) and value[index] != '"':
        character = value[index]
        if character == "\\":
            index += 1
            if index == len(value) or value[index] not in {"\\", '"', "`", "$"}:
                raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
            character = value[index]
        characters.append(character)
        index += 1
    if index == len(value):
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    index += 1
    if index < len(value) and value[index] != " ":
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    return "".join(characters), index


def _unquoted_desktop_argument(value: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(value) and value[index] != " ":
        if value[index] in _DESKTOP_RESERVED_CHARACTERS:
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        index += 1
    return value[start:index], index


def _desktop_string_unescape(value: str) -> str:
    characters: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            characters.append(character)
            index += 1
            continue
        index += 1
        if index == len(value) or value[index] != "\\":
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        characters.append("\\")
        index += 1
    return "".join(characters)


def _write_login_attempt(state_path: Path, attempt: LoginAttempt) -> None:
    payload: dict[str, object] = {
        "version": 1,
        "attempt_id": attempt.attempt_id,
        "started_at": attempt.started_at,
        "state": attempt.state,
        "outcomes": [
            {"project": project, "outcome": outcome.value} for project, outcome in attempt.outcomes
        ],
    }
    if attempt.failure is not None:
        payload["failure"] = attempt.failure
    if attempt.completed_at is not None:
        payload["completed_at"] = attempt.completed_at
    _atomic_write(state_path, (json.dumps(payload, sort_keys=True) + "\n").encode())


def _atomic_write(path: Path, contents: bytes) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except OSError as error:
        raise WorkspaceError("cannot write login recovery state") from error
    finally:
        if temporary_name is not None and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _login_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _write_config(config_path: Path, projects: Mapping[str, WorkspaceProject]) -> None:
    records = [f"version = {_CONFIG_VERSION}", ""]
    for key, project in projects.items():
        records.extend(
            (
                f"[projects.{json.dumps(key)}]",
                f"path = {json.dumps(str(project.directory))}",
                f"session_id = {json.dumps(project.session_id)}",
                f"agent = {json.dumps(project.agent)}",
                f"provider = {json.dumps(project.provider.value)}",
            )
        )
        if project.model is not None:
            records.append(f"model = {json.dumps(project.model)}")
        if project.external_process is not None:
            records.append(
                "external_process = { "
                f"boot_id = {json.dumps(project.external_process.boot_id)}, "
                f"pid = {project.external_process.pid}, "
                f"start_time = {project.external_process.start_time} "
                "}"
            )
        records.append("")
    content = "\n".join(records)
    descriptor, temporary_name = tempfile.mkstemp(prefix="workspace.", dir=config_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, config_path)
    except OSError as error:
        raise WorkspaceError(
            f"cannot write workspace configuration {config_path}: {error}"
        ) from error
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
