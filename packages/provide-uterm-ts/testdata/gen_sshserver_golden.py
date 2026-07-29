#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the SSH server's admission rules.

An SSH server that accepts any password is a reasonable thing on a loopback
bind — a gateway authenticating at the session layer above it — and a very bad
one on a routable address. That difference is the unit.

* **No validators on a routable bind is refused**, unless an operator writes
  ``allow_unauthenticated`` down. Both validators absent means every password
  and every key is accepted, so the only thing standing between the world and
  a shell would be the bind address.
* **A host key must be private.** Mode 0600 and owned by the current user, or
  it is not loaded: a key another account can read is a key another account can
  impersonate the server with.
* **Connections are counted per address**, so one host cannot exhaust the
  server's capacity on its own.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_sshserver_golden.py
"""

from __future__ import annotations

import json
import stat as stat_module
import tempfile
from pathlib import Path
from typing import Any

from provide.uterm.transports import ssh as ssh_transport

OUT = Path(__file__).resolve().parent / "sshserver_golden.json"

HOSTS: list[tuple[str, str]] = [
    ("loopback, written out", "127.0.0.1"),
    ("another loopback address", "127.0.0.53"),
    ("loopback, by name", "localhost"),
    ("loopback, by name in capitals", "LOCALHOST"),
    ("loopback in IPv6", "::1"),
    ("every address", "0.0.0.0"),
    ("every address in IPv6", "::"),
    ("a private address", "10.0.0.5"),
    ("a public address", "93.184.216.34"),
    ("a hostname", "ssh.example.com"),
    ("nothing at all", ""),
    ("a name that merely contains localhost", "notlocalhost"),
    ("a name under the localhost subtree", "api.localhost"),
]

# password validator, key validator, allow_unauthenticated, host
ADMISSION: list[tuple[str, bool, bool, bool, str]] = [
    ("no validators on loopback", False, False, False, "127.0.0.1"),
    ("no validators on loopback by name", False, False, False, "localhost"),
    ("no validators on every address", False, False, False, "0.0.0.0"),
    ("no validators on a public address", False, False, False, "93.184.216.34"),
    ("no validators on a hostname", False, False, False, "ssh.example.com"),
    ("no validators, opted in, on a public address", False, False, True, "93.184.216.34"),
    ("a password validator on a public address", True, False, False, "93.184.216.34"),
    ("a key validator on a public address", False, True, False, "93.184.216.34"),
    ("both validators on a public address", True, True, False, "93.184.216.34"),
    ("no validators on an empty host", False, False, False, ""),
]


def _admission(has_password: bool, has_key: bool, allow_unauthenticated: bool, host: str) -> bool:
    """Whether ``start_ssh_server`` would refuse this combination.

    Transcribed from the one condition it raises on, and recorded so a port
    cannot quietly drop a term: driving the real function would need a live
    listener and a host key.
    """
    return not (
        not has_password and not has_key and not allow_unauthenticated and not ssh_transport._is_loopback_bind(host)
    )


def _permissions() -> list[dict[str, Any]]:
    """What the key-permission check does with each mode, actually run."""
    results = []
    with tempfile.TemporaryDirectory() as directory:
        for name, mode in (
            ("private to its owner", 0o600),
            ("readable by the group", 0o640),
            ("readable by everybody", 0o644),
            ("writable by everybody", 0o666),
            ("executable as well", 0o700),
            ("read-only to its owner", 0o400),
            ("nothing at all", 0o000),
        ):
            path = Path(directory) / f"key-{mode:o}"
            path.write_text("key material")
            path.chmod(mode)
            try:
                ssh_transport._verify_key_permissions(path)
                results.append({"name": name, "mode": mode, "error": None})
            except PermissionError as exc:
                results.append({"name": name, "mode": mode, "error": str(exc).split(":")[0]})
            finally:
                path.chmod(0o600)
    return results


class _Server:
    """The connection counter, driven through the real class."""

    def __init__(self, counts: dict[str, int], limit: int) -> None:
        self.server = ssh_transport.TerminalSSHServer(counts, limit)


def _connections() -> list[dict[str, Any]]:
    """Whether an address may open one more connection, at and past the limit."""
    results = []
    for limit in (1, 5):
        for existing in range(limit + 2):
            counts = {"203.0.113.7": existing} if existing else {}
            # The check the reference makes in ``connection_made``.
            allowed = counts.get("203.0.113.7", 0) < limit
            results.append({"limit": limit, "existing": existing, "allowed": allowed})
    return results


def main() -> None:
    corpus = {
        "default_max_connections_per_ip": 5,
        "required_key_mode": 0o600,
        "loopback": [
            {"name": name, "host": host, "loopback": ssh_transport._is_loopback_bind(host)} for name, host in HOSTS
        ],
        "admission": [
            {
                "name": name,
                "has_password_validator": has_password,
                "has_key_validator": has_key,
                "allow_unauthenticated": allow,
                "host": host,
                "allowed": _admission(has_password, has_key, allow, host),
            }
            for name, has_password, has_key, allow, host in ADMISSION
        ],
        "key_permissions": _permissions(),
        "connections": _connections(),
        "stat_mode_mask": stat_module.S_IMODE(0o100600),
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['admission'])} admission cases)")


if __name__ == "__main__":
    main()
