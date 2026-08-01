#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript SSH server policy.

The SSH *server* is asyncssh-shaped and stays in Python; what has to hold in
every port is the policy around it, because each rule here is one that fails
open when it is wrong:

* **Permissive authentication.** With no validators the server accepts any
  credential. That is legitimate for a gateway that authenticates at the
  session layer — but only on a loopback bind, or with an explicit opt-in. Get
  the loopback test wrong and an "accept anything" server is listening on a
  public interface.
* **Host key permissions.** A private key that is world-readable, or owned by
  someone else, is not a secret. Loading it anyway would be silent.
* **Per-IP connection limits.** The counter has to come back down when a
  connection ends, or a single client eventually locks itself — and everyone
  behind the same NAT — out of the server for good.

The corpus is recorded by driving the real functions, so the loopback dialect
(what counts as a loopback address, and what "localhost" means) is CPython's
`ipaddress` rather than a second reading of it.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_sshpolicy_golden.py
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from provide.uterm.transports import ssh as ssh_module

OUT = Path(__file__).with_name("sshpolicy_golden.json")

# The uids the ownership refusal is recorded with. They are stated here rather
# than read off the machine because the refusal quotes both of them, and the
# real uids are a fact about whoever ran the generator — 501 on a Mac, 1001 on
# a CI runner, 0 in a container. Recording those made the corpus reproduce on
# exactly one laptop and the drift check went red everywhere else. What the
# port has to match is the *shape* of the message, so the numbers in it are
# fixed. Deliberately not any uid a real account is likely to hold, so nobody
# reads them as something that was observed. Do not go back to `os.getuid()`.
OWNER_UID = 4242
CURRENT_UID = 4243

# The path the ownership refusal names, for the same reason: a temporary
# directory is a different string on every run.
FOREIGN_KEY_PATH = "/keys/ssh_host_key"

# Bind addresses, including the ones that look loopback and are not.
HOSTS: list[str] = [
    "127.0.0.1",
    "127.0.0.2",
    "127.255.255.254",
    "::1",
    "localhost",
    "LOCALHOST",
    "localhost.localdomain",
    "0.0.0.0",  # the all-interfaces bind is exactly what must not count as loopback
    "::",
    "10.0.0.1",
    "192.168.1.10",
    "example.org",
    "",
    "127.0.0.1.",
    "0177.0.0.1",
    "2130706433",
    "::ffff:127.0.0.1",
    "fe80::1",
]

# (name, mode) — the file modes a host key may be found with.
KEY_MODES: list[tuple[str, int]] = [
    ("owner read write", 0o600),
    ("owner read only", 0o400),
    ("group readable", 0o640),
    ("world readable", 0o644),
    ("world writable", 0o666),
    ("executable", 0o700),
    ("no permissions", 0o000),
]

# (name, kwargs) — every combination the start guard distinguishes.
START_CASES: list[tuple[str, dict[str, Any]]] = [
    ("no validators on a public bind", {"host": "0.0.0.0"}),  # the case under test
    ("no validators on loopback", {"host": "127.0.0.1"}),
    ("no validators on localhost", {"host": "localhost"}),
    ("a password validator on a public bind", {"host": "0.0.0.0", "credentials_validator": lambda *_: True}),
    ("a key validator on a public bind", {"host": "0.0.0.0", "public_key_validator": lambda *_: True}),
    ("an explicit opt-in on a public bind", {"host": "0.0.0.0", "allow_unauthenticated": True}),
    ("no validators on a private range", {"host": "10.0.0.1"}),
]


class FakePeer:
    """A connection whose peer address is whatever the case asked for."""

    def __init__(self, peer: tuple[str, int] | None) -> None:
        self.peer = peer
        self.closed = False

    def get_extra_info(self, name: str) -> Any:
        """Return the peer address, as asyncssh does."""
        return self.peer if name == "peername" else None

    def close(self) -> None:
        """Record the rejection."""
        self.closed = True


def _record_loopback() -> list[dict[str, Any]]:
    """Record which bind addresses count as loopback."""
    return [{"host": host, "loopback": ssh_module._is_loopback_bind(host)} for host in HOSTS]


def _record_key_permissions() -> list[dict[str, Any]]:
    """Record what each host-key file mode does.

    Real files with real modes, so what is recorded is what ``chmod`` and
    ``stat`` actually produce rather than a second reading of the mode bits.
    Only the *ownership* half of the check is pinned: ``os.getuid`` is held at
    whatever uid the temporary file came out owned by, so the reference gets
    past the owner check on every machine and each row says something about
    its mode and nothing about who ran the generator. Without that pin the 0600
    row would carry a uid the moment the two ever disagreed.
    """
    records = []
    real_getuid = os.getuid
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        for name, mode in KEY_MODES:
            key_path = directory / f"key_{mode:04o}"
            key_path.write_bytes(b"not-a-real-key")
            key_path.chmod(mode)
            owner = key_path.stat().st_uid
            os.getuid = lambda owner=owner: owner  # type: ignore[assignment,misc]
            row: dict[str, Any] = {"name": name, "mode": mode, "mode_repr": oct(mode)}
            try:
                ssh_module._verify_key_permissions(key_path)
            except PermissionError as exc:
                # The path is the caller's temporary directory, so only the
                # part of the message that is about the mode is recorded.
                row["error"] = str(exc).split(":")[0]
            else:
                row["error"] = None
            finally:
                os.getuid = real_getuid  # type: ignore[assignment]
            records.append(row)
    return records


class StatedKey:
    """A host key whose mode and owner are stated rather than found on disk.

    ``_verify_key_permissions`` reads exactly two facts off the path it is
    given — ``st_mode`` and ``st_uid`` — and quotes the path itself in the
    refusal. Supplying all three drives the real reference function while
    leaving nothing in the recording that came from this machine.
    """

    def __init__(self, path: str, mode: int, uid: int) -> None:
        self.path = path
        self.mode = mode
        self.uid = uid

    def stat(self) -> os.stat_result:
        """Return the stated mode and owner, as :meth:`Path.stat` would."""
        return os.stat_result((stat.S_IFREG | self.mode, 0, 0, 1, self.uid, 0, 0, 0, 0, 0))

    def __str__(self) -> str:
        """Name the file, which the refusal quotes."""
        return self.path


def _record_foreign_owner() -> dict[str, Any]:
    """Record the refusal when the key belongs to someone else.

    Both uids are the fixed placeholders above, so the recorded message is the
    same bytes on every machine. The owner and the current uid are recorded
    alongside it so the port can reproduce the message from them rather than
    hard-coding the two numbers a second time.
    """
    real_getuid = os.getuid
    os.getuid = lambda: CURRENT_UID  # type: ignore[assignment]
    try:
        ssh_module._verify_key_permissions(StatedKey(FOREIGN_KEY_PATH, 0o600, OWNER_UID))  # type: ignore[arg-type]
    except PermissionError as exc:
        message = str(exc)
    else:
        message = ""
    finally:
        os.getuid = real_getuid  # type: ignore[assignment]
    return {"message": message, "owner_uid": OWNER_UID, "current_uid": CURRENT_UID, "path": FOREIGN_KEY_PATH}


async def _record_start_guard() -> list[dict[str, Any]]:
    """Record which start-up configurations are refused, and why."""
    created: list[dict[str, Any]] = []

    async def fake_create_server(*args: Any, **kwargs: Any) -> str:
        created.append({"host": args[1], "port": args[2]})
        return "server"

    real_create = ssh_module.asyncssh.create_server
    real_key = ssh_module._get_or_create_host_key
    ssh_module.asyncssh.create_server = fake_create_server  # type: ignore[assignment]
    ssh_module._get_or_create_host_key = lambda *_: "host-key"  # type: ignore[assignment]
    records = []
    try:
        for name, kwargs in START_CASES:

            async def handler(reader: Any, writer: Any) -> None:  # pragma: no cover - never called here
                return None

            try:
                await ssh_module.start_ssh_server(handler, host_key_path=Path("/tmp"), **kwargs)
            except RuntimeError as exc:
                records.append({"name": name, "refused": True, "message": str(exc)})
            else:
                records.append({"name": name, "refused": False, "message": None})
    finally:
        ssh_module.asyncssh.create_server = real_create  # type: ignore[assignment]
        ssh_module._get_or_create_host_key = real_key  # type: ignore[assignment]
    return records


def _record_per_ip_limit() -> dict[str, Any]:
    """Record the accept/reject sequence a per-IP limit produces."""
    connections: dict[str, int] = {}
    limit = 2
    servers = []
    events = []

    # Three from one address, one from another: the fourth is the one the
    # limit is for, and the other address must be unaffected by it.
    for peer in [("10.0.0.1", 5000), ("10.0.0.1", 5001), ("10.0.0.1", 5002), ("10.0.0.2", 5003)]:
        server = ssh_module.TerminalSSHServer(connections, limit)
        conn = FakePeer(peer)
        server.connection_made(conn)
        servers.append((server, conn))
        events.append({"peer": list(peer), "rejected": conn.closed, "counts": dict(connections)})

    # And releasing them puts the counter back, rather than locking the
    # address out for the lifetime of the process.
    for server, _conn in servers:
        server.connection_lost(None)
    events.append({"peer": None, "rejected": False, "counts": dict(connections)})

    unknown = ssh_module.TerminalSSHServer({}, limit)
    unknown_conn = FakePeer(None)
    unknown.connection_made(unknown_conn)

    return {"limit": limit, "events": events, "unknown_peer_rejected": unknown_conn.closed}


def _record_validators() -> dict[str, Any]:
    """Record the permissive defaults and what a validator overrides."""
    permissive = ssh_module.TerminalSSHServer({}, 5)
    strict = ssh_module.TerminalSSHServer(
        {},
        5,
        password_validator=lambda _user, password: password == "letmein",  # noqa: S105  # a fixture, not a credential
        public_key_validator=lambda user, _key: user == "operator",
    )
    return {
        "begin_auth": permissive.begin_auth("anyone"),
        "password_supported": permissive.password_auth_supported(),
        "public_key_supported": permissive.public_key_auth_supported(),
        "permissive_password": permissive.validate_password("anyone", "anything"),
        "permissive_public_key": permissive.validate_public_key("anyone", None),
        "strict_password_right": strict.validate_password("anyone", "letmein"),
        "strict_password_wrong": strict.validate_password("anyone", "hunter2"),
        "strict_key_right": strict.validate_public_key("operator", None),
        "strict_key_wrong": strict.validate_public_key("intruder", None),
    }


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "loopback": _record_loopback(),
        "key_permissions": _record_key_permissions(),
        "foreign_owner": _record_foreign_owner(),
        "start_guard": await _record_start_guard(),
        "per_ip_limit": _record_per_ip_limit(),
        "validators": _record_validators(),
        "expected_mode": 0o600,
        "expected_mode_repr": oct(0o600),
        "default_max_connections_per_ip": 5,
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['loopback'])} hosts, {len(corpus['start_guard'])} start cases)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
