#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""One kept capture session's file: its format, and the files that hold it.

Split out of :mod:`provide.uterm.server.capture_state`, which keeps the
sessions; this is the part the Go port's ``capturestate/record.go`` mirrors.
A file is read back only if it is a record of a format this server reads,
judged by the JSON it holds rather than by what a lenient model would make of
it, so the two ports accept and refuse the same files
(``capture_record_golden.json`` holds the reference's files, refusals and the
files of other builds it reads):

* ``format`` is the compatibility contract: a newer one is refused. Within a
  format, builds may differ in the definition's keys, since an upgrade or a
  downgrade restarts the server on the files the other build wrote. A key
  this build does not know is ignored (never folded into the connector's
  config), and a missing key that has a default takes it; the keys that say
  which login, connector and readers a session has (:data:`REQUIRED_KEYS`)
  are never defaulted;
* every key present is of its own JSON type, integers JSON integers within
  64 bits, and times finite;
* the screen, scrollback and login of exactly the shapes written.
"""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import ValidationError

from provide.uterm.server.config_schema import SessionDefinition

#: The file format; a file naming a newer one is not read. A change no
#: older build could read safely, beyond a definition key added or removed,
#: is a new format.
FORMAT_VERSION = 1

#: The definition's keys a file must have. Their defaults would change what a
#: session connects to and who may read it, so a file without one is refused.
REQUIRED_KEYS = ("session_id", "connector_type", "connector_config", "owner", "visibility")

#: The largest file read or written. A screen is a few tens of KiB and the
#: scrollback at most ``previous_scrollback_lines`` rows of one; this bounds
#: what a corrupt or planted file can make startup read into memory.
MAX_RECORD_BYTES = 16 * 1024 * 1024

#: Names a write in progress; one a crash left behind is removed on load.
TMP_PREFIX = ".tmp-"

# A PAM capture id: pam-<user>-capture-<pid>-<start ticks>. The user is held
# to the characters a session id may carry in every port (ASCII word
# characters and "-"), and the id to the 200 bytes any session id may have.
_PAM_CAPTURE_ID = re.compile(r"\Apam-([A-Za-z0-9_-]+)-capture-([0-9]+)-([0-9]+)\Z")
_MAX_ID_BYTES = 200
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

# A time as RFC 3339 writes it, which is what both ports write a definition's
# created_at as and what Go's time package reads back.
_RFC3339 = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})\Z")


def _is_int(value: object) -> TypeGuard[int]:
    """A JSON integer within 64 bits, as Go's decoder reads one."""
    # ``type`` rather than isinstance: a bool is an int to isinstance, and is not one in JSON.
    return type(value) is int and _INT64_MIN <= value <= _INT64_MAX


def _is_time(value: object) -> bool:
    """A finite, non-negative number of seconds that a float holds, as every port reads it."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        # An integer past any float (10**400), which the Go port reads as infinite.
        return False


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# Each key of a definition, in the order the model declares them, and what its
# JSON value must be.
_SESSION_FIELDS: dict[str, Any] = {
    "session_id": lambda v: isinstance(v, str),
    "display_name": lambda v: isinstance(v, str),
    "connector_type": lambda v: isinstance(v, str),
    "connector_config": lambda v: isinstance(v, dict),
    "input_mode": lambda v: v in ("open", "hijack"),
    "auto_start": lambda v: isinstance(v, bool),
    "tags": _is_str_list,
    "recording_enabled": lambda v: v is None or isinstance(v, bool),
    "created_at": lambda v: isinstance(v, str) and _RFC3339.match(v) is not None,
    "owner": lambda v: v is None or isinstance(v, str),
    "visibility": lambda v: v in ("public", "operator", "private"),
    "ephemeral": lambda v: isinstance(v, bool),
    "presence": lambda v: isinstance(v, bool),
    "auto_transfer_idle_s": _is_int,
    "keystroke_queue": lambda v: v in ("display", "replay"),
}


def login_identity(session_id: str) -> tuple[int, int] | None:
    """The pid and start ticks a PAM capture session's id names, or None for any other id."""
    match = _PAM_CAPTURE_ID.match(session_id)
    if match is None or len(session_id.encode()) > _MAX_ID_BYTES:
        return None
    pid, ticks = int(match.group(2)), int(match.group(3))
    return (pid, ticks) if pid <= _INT64_MAX and ticks <= _INT64_MAX else None


def pam_user(session_id: str) -> str | None:
    """The user a PAM capture id names, or None for any other id."""
    match = _PAM_CAPTURE_ID.match(session_id)
    return None if match is None else match.group(1)


class RecordError(ValueError):
    """A file that is not a record this server wrote."""


@dataclass
class CaptureRecord:
    """One capture session as its file holds it."""

    definition: SessionDefinition
    recording_instance: str
    screen: str | None = None
    cols: int = 0
    rows: int = 0
    scrollback: list[str] = field(default_factory=list)
    saved_at: float = 0.0
    ended_at: float | None = None
    #: The definition keys the file held that this build does not know, read
    #: past: what a newer build of the same format wrote.
    ignored_keys: tuple[str, ...] = field(default=(), compare=False)

    @property
    def session_id(self) -> str:
        return self.definition.session_id

    def summary(self) -> dict[str, Any]:
        """What ``GET /api/previous-sessions`` lists for this record."""
        return {
            "id": self.session_id,
            "display_name": self.definition.display_name,
            "owner": self.definition.owner,
            "ended_at": self.ended_at,
            "cols": self.cols,
            "rows": self.rows,
        }

    def to_json(self) -> dict[str, Any]:
        """The file's content. Only a PAM capture session has one: its id names its login."""
        identity = login_identity(self.session_id)
        if identity is None:
            raise RecordError(f"not a PAM capture session: {self.session_id}")
        data: dict[str, Any] = {
            "format": FORMAT_VERSION,
            "kind": "live" if self.ended_at is None else "previous",
            "session": self.definition.model_dump(mode="json"),
            "recording_instance": self.recording_instance,
            "login": {"pid": identity[0], "start_ticks": identity[1]},
            "screen": None if self.screen is None else {"ansi": self.screen, "cols": self.cols, "rows": self.rows},
            "scrollback": list(self.scrollback),
            "saved_at": self.saved_at,
        }
        if self.ended_at is not None:
            data["ended_at"] = self.ended_at
        return data


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise RecordError(what)


def _parse_session(session: object, session_id: str) -> tuple[SessionDefinition, tuple[str, ...]]:
    """The definition, and the keys it held that this build does not know.

    Refused without a required key, or with any known value not of its JSON
    type; a missing key with a default takes it.
    """
    if not isinstance(session, dict):
        raise RecordError("bad session")
    for name in REQUIRED_KEYS:
        _require(name in session, f"bad session: {name}")
    _require(session["session_id"] == session_id, "session id is not the file's name")
    _require(session["connector_type"] == "pty_capture", "not a capture session")
    known = {name: session[name] for name in _SESSION_FIELDS if name in session}
    for name, value in known.items():
        _require(_SESSION_FIELDS[name](value), f"bad session: {name}")
    ignored = tuple(sorted(name for name in session if name not in _SESSION_FIELDS))
    try:
        return SessionDefinition.model_validate(known), ignored
    except ValidationError as exc:
        # What the shapes cannot see: a date that does not exist, or a
        # connector type this server has not registered.
        raise RecordError(f"bad session: {exc.errors()[0]['loc'][0]}") from None


def parse_record(data: object, *, session_id: str, kind: str) -> CaptureRecord:
    """The record *data* holds, refusing anything this server would not have written.

    The file is named after the session, so the id inside must be that name, a
    PAM capture id whose pid and start ticks are the ``login`` beside it, and
    the definition a capture session's. Its keys are read as the module
    docstring says: a format newer than this build's is refused.
    """
    if not isinstance(data, dict):
        raise RecordError("not an object")
    version = data.get("format")
    if not _is_int(version) or version < 1:
        raise RecordError("unknown format")
    _require(version <= FORMAT_VERSION, f"newer format: {version}")
    _require(data.get("kind") == kind, f"not a {kind} record")
    definition, ignored = _parse_session(data.get("session"), session_id)
    login = data.get("login")
    identity = login_identity(session_id)
    _require(
        identity is not None
        and isinstance(login, dict)
        and set(login) == {"pid", "start_ticks"}
        and all(_is_int(value) for value in login.values())
        and (login["pid"], login["start_ticks"]) == identity,
        "login is not the one the id names",
    )
    instance = data.get("recording_instance")
    if not isinstance(instance, str) or not instance:
        raise RecordError("bad recording instance")
    screen = data.get("screen")
    if screen is not None:
        _require(
            isinstance(screen, dict)
            and set(screen) == {"ansi", "cols", "rows"}
            and isinstance(screen["ansi"], str)
            and _is_int(screen["cols"])
            and _is_int(screen["rows"])
            and screen["cols"] > 0
            and screen["rows"] > 0,
            "bad screen",
        )
    scrollback = data.get("scrollback")
    if not _is_str_list(scrollback):
        raise RecordError("bad scrollback")
    _require(_is_time(data.get("saved_at")), "bad saved_at")
    ended_at = data.get("ended_at")
    _require(("ended_at" in data) == (kind == "previous") and (kind == "live" or _is_time(ended_at)), "bad ended_at")
    return CaptureRecord(
        definition=definition,
        recording_instance=instance,
        screen=None if screen is None else screen["ansi"],
        cols=0 if screen is None else screen["cols"],
        rows=0 if screen is None else screen["rows"],
        scrollback=scrollback,
        saved_at=float(data["saved_at"]),
        ended_at=None if ended_at is None else float(ended_at),
        ignored_keys=ignored,
    )


def _no_constant(name: str) -> object:
    # NaN and Infinity are not JSON, and Go's decoder refuses them. The file
    # is refused as "not an object" whatever is said here, so nothing is.
    raise ValueError


def parse_bytes(content: bytes, *, session_id: str, kind: str) -> CaptureRecord:
    """The record a file's bytes hold: UTF-8 JSON, then :func:`parse_record`.

    Bytes that are not UTF-8, not JSON, or JSON nested past the parser's depth
    are all "not an object", as the Go port words them.
    """
    try:
        # bytes.decode's encoding is UTF-8, always.
        data = json.loads(content.decode(), parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise RecordError("not an object") from None
    return parse_record(data, session_id=session_id, kind=kind)


def errno_name(exc: OSError) -> str:
    """An OS error by its errno name (ELOOP, EACCES), the same words in every port.

    One that carries no errno is its own text, and an errno with no name its
    number, as the Go port says them.
    """
    if exc.errno is None:
        return str(exc)
    return errno.errorcode.get(exc.errno, str(exc.errno))


def read_file(path: Path) -> bytes:
    """The bytes of a record file this process owns, refusing anything else.

    Opened without following a symlink, and without blocking (a FIFO planted
    there would otherwise hold startup until something wrote to it), then
    checked on the open descriptor: a regular file, owned by this process's
    user, no larger than :data:`MAX_RECORD_BYTES`. Another user's file is
    refused even where the directory let them put it there. Every failure is a
    :class:`RecordError`, an I/O error included.
    """
    # POSIX only, as PAM capture is: read inside the call, so importing the
    # module stays safe where the flags do not exist.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RecordError(f"cannot open: {errno_name(exc)}") from None
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise RecordError(f"cannot stat: {errno_name(exc)}") from None
        _require(stat.S_ISREG(info.st_mode), "not a regular file")
        _require(info.st_uid == os.geteuid(), "owned by another user")
        _require(info.st_size <= MAX_RECORD_BYTES, "too large")
        try:
            with os.fdopen(fd, "rb", closefd=False) as handle:
                content = handle.read(MAX_RECORD_BYTES + 1)
        except OSError as exc:
            # A failing disk (EIO) costs this file, never startup or a read.
            raise RecordError(f"cannot read: {errno_name(exc)}") from None
    finally:
        os.close(fd)
    # Read one byte past the limit, so a file that grew after the check is refused too.
    _require(len(content) <= MAX_RECORD_BYTES, "too large")
    return content


def write_file(path: Path, content: bytes) -> None:
    """Replace *path* with *content*: a synced 0600 temporary file renamed over it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=TMP_PREFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(tmp).replace(path)
    except BaseException:
        # The temporary file exists until the rename, the only step after
        # which nothing can fail.
        Path(tmp).unlink()
        raise
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    """Make a rename in *directory* durable; a filesystem that cannot is left as it is."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


#: What :func:`check_private_directory` says of a directory that does not exist.
MISSING = "cannot stat: ENOENT"


def check_private_directory(directory: Path) -> str | None:
    """Why *directory* is not a 0700 directory this user owns, or None when it is.

    Not followed through a symlink. A missing one is :data:`MISSING`.
    """
    try:
        info = directory.lstat()
    except OSError as exc:
        return f"cannot stat: {errno_name(exc)}"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        return "not a directory this user owns"
    mode = stat.S_IMODE(info.st_mode)
    return None if mode == 0o700 else f"mode {mode:04o}, not 0700"


def make_private_directory(directory: Path) -> None:
    """Create *directory* 0700, or refuse one that is not a 0700 directory this user owns.

    Its parent must exist: ``mkdir(parents=True)`` would create the missing
    ones with the umask's mode instead.
    """
    with contextlib.suppress(FileExistsError):
        directory.mkdir(mode=0o700)
        # mkdir's mode is narrowed by the umask, never widened; this makes it exact.
        directory.chmod(0o700)
    problem = check_private_directory(directory)
    if problem is not None:
        raise RecordError(f"{directory}: {problem}")
