#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""PAM capture sessions that outlive a server restart, and the previous sessions they leave.

A capture session is a login that is still running when the server stops: the
producer in it reconnects to the same socket path, but the session definition,
its owner and its screen lived only in this process. With
``session_persistence.persist_capture`` each one is kept in a file, and a
restart re-creates the session under its id from it, while the login is still
alive.

With ``session_persistence.previous_keep_days`` a session that ends leaves a
read-only "previous session" record: its last screen and the rows that scrolled
off above it, for that many days.

Only a capture session PAM created is kept: its id names the login's process by
pid and start time (``pam-<user>-capture-<pid>-<start ticks>``), which is what
proves the login is still the same one after a restart. A pid reused by another
login has another start time.

Files (``0600``, in ``0700`` directories, written to a temporary file that is
synced and renamed over the old one, so a crash leaves the old file or the new
one and never half of either):

* ``<state_dir>/live/<session id>.json`` while the session exists;
* ``<state_dir>/previous/<session id>.json`` once it has ended.

Both hold the screen unredacted: a redacted screen painted back is a different
screen, so this copy is as private as the terminal state the process holds in
memory, which is why it is owner-only. What a reader is shown is redacted on
the way out, as the live session's screen is (``routes/previous_sessions``).
The format lives in :mod:`provide.uterm.server.capture_record`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import time
import uuid
from typing import TYPE_CHECKING, Any

from provide.telemetry import get_logger
from provide.uterm.server.capture_record import (
    FORMAT_VERSION,
    MAX_RECORD_BYTES,
    MISSING,
    TMP_PREFIX,
    CaptureRecord,
    RecordError,
    check_private_directory,
    errno_name,
    login_identity,
    make_private_directory,
    pam_user,
    parse_bytes,
    parse_record,
    read_file,
    write_file,
)
from provide.uterm.server.runtime_helpers import settled_screen_due

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from provide.uterm.server.config_schema import SessionDefinition
    from provide.uterm.server.registry import SessionRegistry

__all__ = [
    "FORMAT_VERSION",
    "MAX_RECORD_BYTES",
    "PREVIOUS_PER_OWNER",
    "PRUNE_INTERVAL_S",
    "SCREEN_SAVE_INTERVAL_S",
    "CaptureRecord",
    "CaptureStateStore",
    "RecordError",
    "ScreenKeeper",
    "build_capture_state",
    "login_identity",
    "parse_record",
    "restore_capture_sessions",
]

logger = get_logger(__name__)

#: The most previous sessions kept for one owner. Days of logins each leave
#: one: past this many the oldest go first, so a user who logs in and out all
#: day cannot fill the disk within the retention period, and twenty still
#: covers several sessions a day for a week. A session with no owner (PAM's,
#: until somebody sets one) counts against the user its PAM id names, so one
#: user's logins never evict another's.
PREVIOUS_PER_OWNER = 20

#: How often, at most, a busy session's screen is written. A screen is saved
#: once output pauses (the recording's settle trigger); this bounds output that
#: never pauses to one write a second, each a rename of a synced file.
SCREEN_SAVE_INTERVAL_S = 1.0

#: How often previous sessions past their retention are removed, besides at startup.
PRUNE_INTERVAL_S = 3600.0

_SECONDS_PER_DAY = 86400


def _owner_key(record: CaptureRecord) -> tuple[str | None, str | None]:
    """Whose previous sessions *record* counts among: its owner, else the PAM user its id names.

    In two places, so an owner never shares a bucket with a PAM user of the same name.
    """
    owner = record.definition.owner
    return (owner, None) if owner is not None else (None, pam_user(record.session_id))


def _encode(data: dict[str, Any]) -> bytes:
    # Compact, as the Go port's encoding/json writes it; str.encode is UTF-8.
    return json.dumps(data, separators=(",", ":")).encode()


def _newest_first(record: CaptureRecord) -> tuple[float, str]:
    # Ties go by id, as in the Go port, so both list and evict the same records.
    return (-(record.ended_at or 0.0), record.session_id)


class CaptureStateStore:
    """The live and previous capture-session files under one directory."""

    def __init__(
        self,
        directory: Path,
        *,
        persist: bool,
        keep_days: int,
        scrollback_lines: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._directory = directory
        self._live_dir = directory / "live"
        self._previous_dir = directory / "previous"
        self._persist = persist
        self._keep_days = keep_days
        self.scrollback_lines = scrollback_lines if keep_days > 0 else 0
        self._clock = clock
        self._records: dict[str, CaptureRecord] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Every previous record's summary fields and definition, without its
        # screen, so a listing reads no file.
        self._previous: dict[str, CaptureRecord] = {}
        # Records adopted from their files whose screen the session's first
        # connector has yet to be given back.
        self._restoring: set[str] = set()

    @property
    def persist(self) -> bool:
        return self._persist

    @property
    def keeps_previous(self) -> bool:
        return self._keep_days > 0

    def tracks(self, definition: SessionDefinition) -> bool:
        """Whether *definition* is a session this store keeps: a PAM capture session."""
        return definition.connector_type == "pty_capture" and login_identity(definition.session_id) is not None

    def track(self, definition: SessionDefinition, recording_instance: str) -> CaptureRecord:
        """Start keeping *definition*'s state in memory, written by the calls below; a kept one is kept.

        Never replaced: a session's record goes only when it ends (:meth:`end`),
        so a second runtime for the same session takes the first one's
        recording instance and screen.
        """
        return self._records.setdefault(
            definition.session_id, CaptureRecord(definition=definition, recording_instance=recording_instance)
        )

    def adopt(self, record: CaptureRecord) -> None:
        """Keep a record restored from its file, as :meth:`track` would."""
        self._records[record.session_id] = record
        self._restoring.add(record.session_id)

    def keeper(self, definition: SessionDefinition) -> ScreenKeeper | None:
        """What keeps *definition*'s screen for its runtime, or None for a session this store does not keep."""
        if not self.tracks(definition):
            return None
        record = self.track(definition, uuid.uuid4().hex)
        restoring = definition.session_id in self._restoring
        self._restoring.discard(definition.session_id)
        return ScreenKeeper(self, record, restore=restoring)

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    async def _write_live(self, record: CaptureRecord) -> None:
        if self._persist:
            await self._write(self._live_dir, record)

    async def _write(self, directory: Path, record: CaptureRecord) -> None:
        """Write *record* into *directory*: encoded here, on the event loop, and written on a thread.

        Encoded here so the definition, which a PATCH changes on the loop, is
        never read from another thread half-changed.
        """
        content = _encode(record.to_json())
        if len(content) > MAX_RECORD_BYTES and record.scrollback:
            # A screen alone fits; it is the scrollback that can outgrow the
            # limit, and the screen is what matters most.
            content = _encode({**record.to_json(), "scrollback": []})
        if len(content) > MAX_RECORD_BYTES:
            raise RecordError(f"{len(content)} bytes is too large")
        failure = await asyncio.to_thread(self._write_bytes, directory / f"{record.session_id}.json", content)
        if failure is not None:
            raise failure

    def _write_bytes(self, path: Path, content: bytes) -> OSError | RecordError | None:
        """Write on a thread, handing a failure back rather than raising it across the await.

        CPython 3.11 stops tracing the awaiting frames once an exception from
        a thread is thrown into them, so coverage there would miss every line
        after the failure; returned and raised on the loop, it is an ordinary
        raise.
        """
        try:
            make_private_directory(self._directory)
            make_private_directory(path.parent)
            write_file(path, content)
        except (OSError, RecordError) as exc:
            return exc
        return None

    async def save_definition(self, session_id: str) -> None:
        """Write the session's definition, as it now stands, to its file."""
        async with self._lock(session_id):
            record = self._records.get(session_id)
            if record is not None:
                await self._guarded(record, self._write_live(record))

    async def save_screen(self, session_id: str, snapshot: dict[str, Any], scrollback: list[str]) -> None:
        """Keep the screen a connector snapshot shows, and write it when persisting."""
        async with self._lock(session_id):
            record = self._records.get(session_id)
            if record is None:
                return
            record.screen = str(snapshot["screen"])
            record.cols = int(snapshot["cols"])
            record.rows = int(snapshot["rows"])
            record.scrollback = scrollback[-self.scrollback_lines :] if self.scrollback_lines else []
            record.saved_at = self._clock()
            await self._guarded(record, self._write_live(record))

    async def _guarded(self, record: CaptureRecord, write: Awaitable[None]) -> bool:
        """Await *write*; a failure is logged and answered False rather than raised.

        A file that cannot be written costs a restart its copy of the session,
        never the session itself.
        """
        try:
            await write
        except (OSError, RecordError) as exc:
            logger.warning("capture_state_write_failed session_id=%s error=%s", record.session_id, exc)
            return False
        return True

    async def end(self, session_id: str) -> None:
        """The session is gone: keep it as a previous session, or forget it."""
        async with self._lock(session_id):
            record = self._records.pop(session_id, None)
            if record is not None:
                await self._end(record, self._clock())
        # Made by the lock above, so always there.
        del self._locks[session_id]

    async def end_record(self, record: CaptureRecord) -> None:
        """A login found gone at startup ended, at the latest, when its screen was last saved."""
        await self._end(record, record.saved_at)

    def _cutoff(self) -> float:
        """The end time a previous session must not be older than."""
        return self._clock() - self._keep_days * _SECONDS_PER_DAY

    async def _end(self, record: CaptureRecord, ended_at: float) -> None:
        # One that ended longer ago than it would be kept (a login gone during
        # a long downtime) is not written only to be pruned.
        if self.keeps_previous and record.screen is not None and ended_at >= self._cutoff():
            record.ended_at = ended_at
            if await self._guarded(record, self._write(self._previous_dir, record)):
                # The listing holds no screen; a read takes it from the file.
                self._previous[record.session_id] = _without_screen(record)
                self._prune_owner(_owner_key(record))
        with contextlib.suppress(OSError):
            # A missing file is an OSError too.
            (self._live_dir / f"{record.session_id}.json").unlink()

    def _load(self, directory: Path, kind: str) -> list[CaptureRecord]:
        """Every record of *kind* in *directory*; a directory or file that will not do is skipped with a warning.

        Never raises: a server whose state cannot be read starts without it.
        """
        problem = check_private_directory(directory)
        if problem == MISSING:
            return []
        listing = _listing(directory) if problem is None else problem
        if isinstance(listing, str):
            logger.warning("capture_state_dir_ignored path=%s error=%s", str(directory), listing)
            return []
        records: list[CaptureRecord] = []
        for path in listing:
            if path.name.startswith(TMP_PREFIX):
                # Left by a write the process did not live to finish.
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if path.suffix != ".json":
                continue
            try:
                record = _parse_file(path, path.stem, kind)
            except RecordError as exc:
                logger.warning("capture_state_file_ignored path=%s error=%s", str(path), exc)
                continue
            if record.ignored_keys:
                # A newer build of the same format wrote it: expected on a
                # downgrade, so not a warning. Once a file, at load.
                logger.info("capture_state_keys_ignored path=%s keys=%s", str(path), ",".join(record.ignored_keys))
            records.append(record)
        return records

    def load_live(self) -> list[CaptureRecord]:
        """Every live record on disk; a file that is not one is skipped with a warning."""
        return self._load(self._live_dir, "live")

    def load_previous(self) -> None:
        """Read the previous records into the listing, then prune them."""
        self._previous = {
            record.session_id: _without_screen(record) for record in self._load(self._previous_dir, "previous")
        }
        self.prune()

    def prune(self) -> None:
        """Remove previous records past ``previous_keep_days``, then past the per-owner cap."""
        cutoff = self._cutoff()
        for record in list(self._previous.values()):
            if (record.ended_at or 0.0) < cutoff:
                self._drop_previous(record.session_id)
        for owner in {_owner_key(record) for record in self._previous.values()}:
            self._prune_owner(owner)

    def _prune_owner(self, owner: tuple[str | None, str | None]) -> None:
        mine = sorted((record for record in self._previous.values() if _owner_key(record) == owner), key=_newest_first)
        for record in mine[PREVIOUS_PER_OWNER:]:
            self._drop_previous(record.session_id)

    def _drop_previous(self, session_id: str) -> None:
        # Always a listed one: prune walks the listing.
        del self._previous[session_id]
        with contextlib.suppress(OSError):
            (self._previous_dir / f"{session_id}.json").unlink()

    def previous_sessions(self) -> list[CaptureRecord]:
        """Every previous record, newest first (ties by id), without its screen."""
        return sorted(self._previous.values(), key=_newest_first)

    def previous_summary(self, session_id: str) -> CaptureRecord | None:
        """The listing's copy of *session_id*'s previous record (no screen), or None."""
        return self._previous.get(session_id)

    async def previous_session(self, session_id: str) -> CaptureRecord | None:
        """The previous record of *session_id*, screen included, or None.

        Read and parsed on a thread: the file can be megabytes, and the event
        loop serves every terminal.
        """
        if session_id not in self._previous:
            return None
        path = self._previous_dir / f"{session_id}.json"
        # A refusal comes back as a value, not raised across the await (see
        # _write_bytes).
        read = await asyncio.to_thread(_parse_file_or_refusal, path, session_id, "previous")
        if isinstance(read, RecordError):
            logger.warning("capture_state_file_ignored path=%s error=%s", str(path), read)
            return None
        return read

    async def update_previous(self, session_id: str, changes: dict[str, Any]) -> CaptureRecord | None:
        """Set *changes* (already-validated definition fields) on a previous record; its listing copy, or None.

        The file is read, changed and rewritten whole, under the session's
        lock, so the screen it holds is kept. None when the record is not
        listed, when its file has gone or turned unreadable since (its listing
        entry is dropped, since nobody can read it any more), or when the new
        owner's cap removed it. Raises :class:`RecordError` when the new file
        could not be written, which leaves the old one and the listing as they
        were.
        """
        async with self._lock(session_id):
            record = await self.previous_session(session_id)
            if record is None:
                self._previous.pop(session_id, None)
                return None
            changed = dataclasses.replace(record, definition=record.definition.model_copy(update=changes))
            if not await self._guarded(changed, self._write(self._previous_dir, changed)):
                # Bare: _guarded has logged why, and the caller answers for itself.
                raise RecordError
            self._previous[session_id] = _without_screen(changed)
            self._prune_owner(_owner_key(changed))
            return self._previous.get(session_id)


class ScreenKeeper:
    """Keeps one capture session's screen in its store record, as the screen changes.

    A second sink on the recording's settle trigger (:func:`settled_screen_due`)
    with none of the recording's gates: it runs with no recording, past the
    recording's quota, and keeps the screen unredacted.
    """

    def __init__(self, store: CaptureStateStore, record: CaptureRecord, *, restore: bool) -> None:
        self._store = store
        self._record = record
        self._restore = restore
        # Paint the restored screen into the first connector only.
        self._attaches = 0
        # Output owed a save: (when it began, the latest), or None when none is.
        self._pending: tuple[float, float] | None = None
        self._last_saved_at = -math.inf
        self._last_saved: str | None = None

    @property
    def recording_instance(self) -> str:
        """The recording instance the session records under, kept across a restart."""
        return self._record.recording_instance

    def attach(self, connector: Any) -> None:
        """Ready a freshly started connector: the scrollback kept so far, and the screen a restart left.

        The scrollback carries over every start, a stop and start in this
        process as much as a restart, so a previous session keeps all of it.
        """
        self._attaches += 1
        connector.keep_scrollback(self._store.scrollback_lines, self._record.scrollback)
        if self._restore and self._attaches == 1 and self._record.screen is not None:
            connector.restore_screen(self._record.screen, self._record.cols, self._record.rows)
            self._last_saved = self._record.screen

    def note_output(self, now: float) -> None:
        self._pending = (now if self._pending is None else self._pending[0], now)

    async def save_if_due(self, connector: Any, now: float) -> None:
        """Save the screen when the settle trigger says one is owed and it has changed."""
        if self._pending is None:
            return
        since, last_output = self._pending
        if not settled_screen_due(
            now,
            pending_since=since,
            last_output_at=last_output,
            last_taken_at=self._last_saved_at,
            interval=SCREEN_SAVE_INTERVAL_S,
        ):
            return
        self._last_saved_at = now
        self._pending = None
        snapshot = await self._snapshot(connector)
        if snapshot is not None and snapshot["screen"] != self._last_saved:
            await self._save(connector, snapshot)

    async def save_now(self, connector: Any) -> None:
        """Save the screen whatever the trigger says: the session is stopping."""
        snapshot = await self._snapshot(connector)
        if snapshot is not None:
            await self._save(connector, snapshot)

    async def _snapshot(self, connector: Any) -> dict[str, Any] | None:
        try:
            return dict(await connector.get_snapshot())
        except Exception as exc:
            # The type alone: the text can quote what the connector was handed.
            logger.warning(
                "capture_state_screen_failed session_id=%s error=%s", self._record.session_id, type(exc).__name__
            )
            return None

    async def _save(self, connector: Any, snapshot: dict[str, Any]) -> None:
        self._last_saved = str(snapshot["screen"])
        await self._store.save_screen(self._record.session_id, snapshot, connector.scrollback())


def login_is_alive(session_id: str) -> bool:
    """Whether the process a PAM capture id names still runs: its pid reports the same start ticks."""
    from provide.uterm.server.pam_integration import pam_login_alive

    identity = login_identity(session_id)
    return identity is not None and pam_login_alive(*identity)


async def restore_capture_sessions(
    registry: SessionRegistry, store: CaptureStateStore | None, socket_allowed: Callable[[str], bool]
) -> None:
    """Re-create every persisted capture session whose login is still alive; end the rest.

    Run at startup, after the control plane's migration and before sessions
    auto-start. A file is trusted no further than a request would be: its
    socket must pass the confinement a PAM-created capture session passes, and
    its login must be the same process, by pid and start time. Anything else
    has ended as far as this server can tell, and becomes a previous session.
    """
    if store is None:
        return
    store.load_previous()
    if not store.persist:
        return
    for record in store.load_live():
        session_id = record.session_id
        if not socket_allowed(str(record.definition.connector_config.get("socket_path", ""))):
            logger.warning("capture_state_restore_refused session_id=%s reason=socket_not_confined", session_id)
            await store.end_record(record)
        elif not login_is_alive(session_id):
            logger.info("capture_state_login_gone session_id=%s", session_id)
            await store.end_record(record)
        elif await registry.adopt_restored(record):
            logger.info("capture_state_restored session_id=%s", session_id)


def _parse_file(path: Path, session_id: str, kind: str) -> CaptureRecord:
    return parse_bytes(read_file(path), session_id=session_id, kind=kind)


def _parse_file_or_refusal(path: Path, session_id: str, kind: str) -> CaptureRecord | RecordError:
    try:
        return _parse_file(path, session_id, kind)
    except RecordError as exc:
        return exc


def _listing(directory: Path) -> list[Path] | str:
    """The entries of *directory*, sorted, or why it cannot be listed."""
    try:
        return sorted(directory.iterdir())
    except OSError as exc:
        return f"cannot list: {errno_name(exc)}"


def _without_screen(record: CaptureRecord) -> CaptureRecord:
    """*record* as a listing holds it: everything but its screen and scrollback, which a read takes from the file."""
    return dataclasses.replace(record, screen=None, scrollback=[])


def build_capture_state(config: Any) -> CaptureStateStore | None:
    """The store ``config`` asks for in ``session_persistence.state_dir``; None when both features are off.

    The directory is made 0700 if it is missing, and refused, which stops the
    server starting, if it is not a 0700 directory this user owns: the files
    in it hold unredacted screens.
    """
    settings = config.session_persistence
    if not settings.persist_capture and settings.previous_keep_days == 0:
        return None
    try:
        make_private_directory(settings.state_dir)
    except RecordError as exc:
        raise ValueError(f"session_persistence.state_dir {exc}") from None
    except OSError as exc:
        raise ValueError(
            f"session_persistence.state_dir {settings.state_dir}: cannot create: {errno_name(exc)}"
        ) from None
    return CaptureStateStore(
        settings.state_dir,
        persist=settings.persist_capture,
        keep_days=settings.previous_keep_days,
        scrollback_lines=settings.previous_scrollback_lines,
    )
