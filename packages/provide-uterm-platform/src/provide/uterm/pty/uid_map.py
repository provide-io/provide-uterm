#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pwd
from dataclasses import dataclass

from provide.uterm.pty._validate import validate_username


@dataclass
class ResolvedUser:
    uid: int
    gid: int
    home: str
    shell: str  # nosec B604 — dataclass field, not subprocess shell=True
    name: str  # OS username (used for os.initgroups)


class UidMapError(ValueError):
    pass


class UidMap:
    """
    Resolve an application username to OS (uid, gid, home, shell).

    Resolution priority:
    1. run_as_uid (per-session explicit uid)
    2. run_as (per-session OS username, numeric uid, or "uid:gid")
    3. _table entry keyed on username (same format as run_as; "*" is wildcard)
    4. pwd.getpwnam(username) — user runs as themselves
    """

    def __init__(self, table: dict[str, str] | None = None, *, allow_root: bool = False) -> None:
        self._table: dict[str, str] = table or {}
        self._allow_root = allow_root

    def resolve(
        self,
        username: str,
        *,
        run_as: str | None = None,
        run_as_uid: int | None = None,
        run_as_gid: int | None = None,
    ) -> ResolvedUser:
        # Validate username early — before touching pwd or the table
        if username:
            validate_username(username)

        if run_as_uid is not None:
            self._check_privilege(run_as_uid, run_as_gid)
            return self._from_uid(run_as_uid, run_as_gid)

        if run_as is not None:
            return self._resolve_spec(run_as, run_as_gid=run_as_gid)

        spec = self._table.get(username) or self._table.get("*")
        if spec is not None:
            return self._resolve_spec(spec, run_as_gid=run_as_gid)

        try:
            pw = pwd.getpwnam(username)
        except KeyError as err:
            raise UidMapError(f"no such OS user: {username!r}") from err

        gid = run_as_gid if run_as_gid is not None else pw.pw_gid
        self._check_privilege(pw.pw_uid, gid)

        return ResolvedUser(  # nosec B604 — shell= is a dataclass field, not subprocess
            uid=pw.pw_uid,
            gid=gid,
            home=pw.pw_dir,
            shell=pw.pw_shell,
            name=pw.pw_name,
        )

    def _check_privilege(self, uid: int, gid: int | None) -> None:
        if self._allow_root:
            return
        if uid == 0 or (gid is not None and gid == 0):
            raise UidMapError(f"resolving to privileged {uid}:{gid} is not allowed")

    def _from_uid(self, uid: int, gid: int | None) -> ResolvedUser:
        self._check_privilege(uid, gid)
        try:
            pw = pwd.getpwuid(uid)
            resolved_gid = gid if gid is not None else pw.pw_gid
            return ResolvedUser(  # nosec B604 — shell= is a dataclass field
                uid=pw.pw_uid,
                gid=resolved_gid,
                home=pw.pw_dir,
                shell=pw.pw_shell,
                name=pw.pw_name,
            )
        except KeyError:
            resolved_gid = gid if gid is not None else uid
            return ResolvedUser(  # nosec B604 — shell= is a dataclass field
                uid=uid,
                gid=resolved_gid,
                home="/",
                shell="/bin/sh",
                name=str(uid),
            )

    def _resolve_spec(self, spec: str, *, run_as_gid: int | None) -> ResolvedUser:
        """Parse: OS-username | "uid" | "uid:gid"."""
        if ":" in spec:
            uid_s, gid_s = spec.split(":", 1)
            uid, gid = int(uid_s), int(gid_s)
            return self._from_uid(uid, gid)

        try:
            uid = int(spec)
            return self._from_uid(uid, run_as_gid)
        except ValueError:
            pass

        try:
            pw = pwd.getpwnam(spec)
        except KeyError as err:
            raise UidMapError(f"no such OS user: {spec!r}") from err
        gid = run_as_gid if run_as_gid is not None else pw.pw_gid
        self._check_privilege(pw.pw_uid, gid)
        return ResolvedUser(  # nosec B604 — shell= is a dataclass field
            uid=pw.pw_uid,
            gid=gid,
            home=pw.pw_dir,
            shell=pw.pw_shell,
            name=pw.pw_name,
        )
