#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Startup scan of login records left by ``pam_uterm``.

When uterm restarts -- an upgrade, a crash, a clean shutdown -- a live SSH login
is still alive (the SSH connection did not drop), but uterm's in-memory session
registry is gone. ``restore_capture_sessions`` re-creates sessions uterm
persisted itself; this scan covers the logins uterm never heard of -- the ones
that opened while uterm was down.

In ``mode=capture``, before notifying, ``pam_uterm`` writes the open event's
JSON line to ``<cap_dir>/uterm-cap-<pid>.login``. At startup, after
``restore_capture_sessions`` and before sessions auto-start, this module lists
those records, validates each one, and adopts the logins that pass.

Each record is trusted no further than a PAM event, and less than a file uterm
wrote itself:

1. ``lstat``: a regular file, owned by uid 0, not group- or world-writable, at
   most 16 KiB.
2. Parsed as a ``PamEvent`` with ``event: open``, ``mode: capture``; its
   ``capture_socket`` must be exactly ``<dir>/uterm-cap-<pid>.sock`` for the
   pid in its name and must pass confinement.
3. Its ``start_ticks`` must be present and equal what ``/proc/<pid>/stat``
   reports now. No start ticks, no adoption: a random-tag id cannot be proven.
4. Its id must not already be in the registry (a session ``persist_capture``
   restored is left as restored, with its screen, owner and recording).

A record that passes is handled as PAM's open would have been: the same
``_on_open`` path, so the session is created with decision 1's owner (the hook
is called with ``adopted: true``). A record that fails is logged once at debug
(a dead login's leftover is normal after an sshd crash) or at warning (wrong
owner, wrong shape, socket not confined) and left alone: uterm cannot remove a
root-owned file from a sticky directory, and should not.

The scan runs once, at startup. A login whose open uterm missed while running
(the notify write failed) is not covered; that is a bug to fix in the listener,
not a reason to poll.
"""

from __future__ import annotations

import logging
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from provide.uterm.server.models import PamConfig
    from provide.uterm.server.registry import SessionRegistry

logger = logging.getLogger(__name__)

# A login record filename: uterm-cap-<pid>.login. The pid is what pam_uterm
# wrote (the PAM process's pid), and it is also in the JSON line's "pid" field.
# The two must agree: the file's pid is the trusted one (the filename came from
# pam_uterm's _login_record_path, which used the pid it had at open time).
_LOGIN_RECORD_RE = re.compile(r"^uterm-cap-(?P<pid>\d+)\.login\Z")

# A record larger than this is not a login record: the open event JSON line is
# a few hundred bytes at most (username, tty, pid, capture_socket path,
# start_ticks, and at most a handful of ssh_auth_info keys). 16 KiB is far
# above any real record and far below a file worth reading to check.
_MAX_LOGIN_RECORD_BYTES = 16 * 1024


def _capture_dir(pam_cfg: PamConfig) -> str | None:
    """The directory to scan for login records.

    Priority: explicit ``capture_socket_dir`` > parent of ``notify_socket`` >
    None (no scan). The notify socket's parent is the fallback because
    ``pam_uterm`` defaults its ``cap_dir`` to ``/run``, the same directory the
    notify socket lives in (``/run/uterm-notify.sock``).
    """
    if pam_cfg.capture_socket_dir:
        return pam_cfg.capture_socket_dir
    if pam_cfg.notify_socket:
        return str(Path(pam_cfg.notify_socket).parent)
    return None


def _expected_capture_socket(cap_dir: str, pid: int) -> str:
    """The capture_socket path the record's pid implies."""
    return f"{cap_dir}/uterm-cap-{pid}.sock"


async def scan_login_records(
    pam_cfg: PamConfig,
    registry: SessionRegistry,
    instances: dict[tuple[str, int], str] | None = None,
    *,
    on_metric: Callable[[str, int], None] | None = None,
) -> tuple[int, int]:
    """Scan login records at startup and adopt logins uterm never heard of.

    Runs after ``restore_capture_sessions`` and before sessions auto-start, only
    when ``pam.mode = "capture"``. Each record that passes the four checks (see
    the module docstring) is handled as PAM's open would have been, via
    ``_on_open`` with ``adopted=True``.

    Returns ``(adopted, skipped)`` -- the count of sessions adopted and the
    count of records that failed a check. A record whose login has ended is
    "skipped" at debug (normal after an sshd crash); a record with the wrong
    owner, shape, or socket is "skipped" at warning.
    """
    if pam_cfg.mode != "capture":
        return (0, 0)

    cap_dir = _capture_dir(pam_cfg)
    if cap_dir is None:
        return (0, 0)

    cap_path = Path(cap_dir)
    try:
        entries = sorted(cap_path.iterdir())
    except (OSError, FileNotFoundError):
        # The directory may not exist yet (no logins have opened, or uterm's
        # capture_socket_dir was never created). Nothing to adopt.
        return (0, 0)

    adopted = 0
    skipped = 0

    # Lazy import: the pam_integration layer is optional in some deployments
    # (it pulls in provide-uterm-platform for PamEvent). The scan is only
    # meaningful when PAM capture mode is configured, which implies the layer
    # is installed.
    from provide.uterm.pty.pam_listener import _parse_event
    from provide.uterm.server.pam_integration import _on_open, _process_start_ticks

    for entry in entries:
        name = entry.name
        match = _LOGIN_RECORD_RE.match(name)
        if match is None:
            continue  # not a login record

        pid = int(match.group("pid"))

        # ── Step 1: lstat ──────────────────────────────────────────────────
        # A regular file, owned by uid 0, not group- or world-writable, at most
        # 16 KiB. A capture-group member can create files in the directory but
        # cannot create one owned by root, so a forged record needs root.
        try:
            st = entry.lstat()
        except OSError:
            skipped += 1
            continue

        if not stat.S_ISREG(st.st_mode):
            logger.warning("pam_capture_adopt_skipped path=%s reason=not_regular_file", entry)
            skipped += 1
            continue

        if st.st_uid != 0:
            logger.warning("pam_capture_adopt_skipped path=%s reason=wrong_owner uid=%d", entry, st.st_uid)
            skipped += 1
            continue

        if st.st_mode & 0o022:  # group- or world-writable
            logger.warning("pam_capture_adopt_skipped path=%s reason=writable mode=%o", entry, st.st_mode & 0o7777)
            skipped += 1
            continue

        if st.st_size > _MAX_LOGIN_RECORD_BYTES:
            logger.warning("pam_capture_adopt_skipped path=%s reason=too_large size=%d", entry, st.st_size)
            skipped += 1
            continue

        # ── Step 2: parse as PamEvent ───────────────────────────────────────
        # Opened O_NOFOLLOW|O_NONBLOCK (lstat already refused a symlink, but
        # the open is still checked in case the file was swapped between the
        # lstat and the open — TOCTOU). The file is read in full; it is at most
        # 16 KiB.
        try:
            data = entry.read_bytes()
        except OSError:
            logger.warning("pam_capture_adopt_skipped path=%s reason=read_failed", entry)
            skipped += 1
            continue

        ev = _parse_event(data)
        if ev is None:
            logger.warning("pam_capture_adopt_skipped path=%s reason=bad_json", entry)
            skipped += 1
            continue

        if ev.event != "open" or ev.mode != "capture":
            logger.warning(
                "pam_capture_adopt_skipped path=%s reason=wrong_event event=%s mode=%s",
                entry,
                ev.event,
                ev.mode,
            )
            skipped += 1
            continue

        # The capture_socket must be exactly <dir>/uterm-cap-<pid>.sock for the
        # pid in its filename, and must be confined to the capture directory.
        expected_socket = _expected_capture_socket(cap_dir, pid)
        if ev.capture_socket != expected_socket:
            logger.warning(
                "pam_capture_adopt_skipped path=%s reason=socket_mismatch expected=%s got=%s",
                entry,
                expected_socket,
                ev.capture_socket,
            )
            skipped += 1
            continue

        # Confinement: the socket path must be inside the capture directory.
        try:
            resolved_socket = Path(ev.capture_socket).resolve()
            resolved_dir = cap_path.resolve()
            if not resolved_socket.is_relative_to(resolved_dir):
                logger.warning(
                    "pam_capture_adopt_skipped path=%s reason=socket_not_confined socket=%s",
                    entry,
                    ev.capture_socket,
                )
                skipped += 1
                continue
        except Exception:
            logger.warning(
                "pam_capture_adopt_skipped path=%s reason=socket_resolve_failed socket=%s",
                entry,
                ev.capture_socket,
            )
            skipped += 1
            continue

        # ── Step 3: start_ticks must match /proc/<pid>/stat now ─────────────
        # No start ticks, no adoption: a random-tag id cannot be proven. The
        # event's start_ticks is what pam_uterm wrote at login time; the current
        # /proc/<pid>/stat start_ticks is what the process reports now. They
        # must be equal — a pid reused by a different process is gone.
        if ev.start_ticks is None:
            logger.debug("pam_capture_adopt_skipped path=%s reason=no_start_ticks", entry)
            skipped += 1
            continue

        current_ticks = _process_start_ticks(pid)
        if current_ticks is None or current_ticks != ev.start_ticks:
            # A dead login's leftover is normal after an sshd crash; debug, not
            # warning.
            logger.debug(
                "pam_capture_adopt_skipped path=%s reason=login_dead pid=%d",
                entry,
                pid,
            )
            skipped += 1
            continue

        # ── Step 4: id not already in the registry ─────────────────────────
        # A session persist_capture restored is left as restored. Build the id
        # the same way _on_open does (it reads /proc, which we just verified).
        from provide.uterm.server.pam_integration import _session_id

        session_id = _session_id(ev)
        existing = await registry.get_definition(session_id)
        if existing is not None:
            logger.debug(
                "pam_capture_adopt_skipped path=%s reason=already_in_registry session_id=%s",
                entry,
                session_id,
            )
            skipped += 1
            continue

        # ── Adopted: the same _on_open path, with adopted=True ──────────────
        try:
            await _on_open(
                ev,
                pam_cfg,
                registry,
                instances=instances,
                on_metric=on_metric,
                adopted=True,
            )
            adopted += 1
            logger.info("pam_capture_adopted session_id=%s pid=%d", session_id, pid)
        except Exception as exc:
            # An adoption that fails to create the session (e.g. the capture
            # socket is already bound, or the registry rejected it) is logged
            # and skipped. The record is left in place; a restart retries it.
            logger.warning(
                "pam_capture_adopt_failed session_id=%s pid=%d error=%s",
                session_id,
                pid,
                exc,
            )
            skipped += 1

    if adopted or skipped:
        logger.info(
            "pam_capture_adopt_scan_complete adopted=%d skipped=%d dir=%s",
            adopted,
            skipped,
            cap_dir,
        )

    return (adopted, skipped)
