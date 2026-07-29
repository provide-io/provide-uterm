#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the MCP authorization policy.

This is the chokepoint an LLM's tool calls pass through, and three decisions
here are the whole of it:

* **Every tool names the role it needs, and an unknown tool raises.** That is
  what stops a newly added tool from slipping through unguarded: the table is
  the single source of truth, and a tool missing from it fails loudly rather
  than defaulting to something permissive.
* **Which host an LLM may point a session at.** The guard refuses loopback,
  link-local and — by default — private ranges *without* resolving DNS, and it
  normalises the non-canonical numeric IPv4 forms a C resolver accepts:
  ``2130706433``, ``0177.0.0.1``, ``0x7f.1``, ``127.1``. A blocklist that only
  understands the dotted quad is trivially bypassed by any of them, and
  ``http://2130706433`` reaches 127.0.0.1.
* **Which connectors may be spawned.** A closed allowlist, because
  ``session_create`` is how an LLM starts a process.

The role ladder and the connector allowlist are recorded whole, so a tool
added on one side and not the other fails the drift check.

# uv-package: provide-uterm-client

Usage (from the repository root)::

    uv run --package provide-uterm-client python \\
        packages/provide-uterm-ts/testdata/gen_mcppolicy_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.ai import constants, policy, server_validators

OUT = Path(__file__).resolve().parent / "mcppolicy_golden.json"

ROLES = ["viewer", "operator", "admin", "", "root", "Admin", "superuser"]

HOSTS: list[tuple[str, str]] = [
    ("an ordinary name", "bbs.example.com"),
    ("loopback, written out", "127.0.0.1"),
    ("loopback, by name", "localhost"),
    ("loopback, by name with a root dot", "localhost."),
    ("loopback, by name in capitals", "LOCALHOST"),
    ("a name under the localhost subtree", "api.localhost"),
    ("a name under the localhost subtree with a root dot", "api.localhost."),
    ("something merely ending in localhost", "notlocalhost"),
    ("the cloud metadata name", "metadata.google.internal"),
    ("the short metadata name", "metadata"),
    ("the cloud metadata address", "169.254.169.254"),
    ("another link-local address", "169.254.1.1"),
    ("a private address", "10.0.0.5"),
    ("another private address", "192.168.1.1"),
    ("a carrier-grade NAT address", "100.64.0.1"),
    ("the unspecified address", "0.0.0.0"),
    ("a reserved address", "240.0.0.1"),
    ("the broadcast address", "255.255.255.255"),
    ("a benchmarking address", "198.18.0.1"),
    ("the IPv6 unspecified address", "::"),
    ("a public address", "93.184.216.34"),
    # The forms a C resolver accepts and a dotted-quad blocklist does not.
    ("loopback as one decimal number", "2130706433"),
    ("loopback in octal", "0177.0.0.1"),
    ("loopback in hex", "0x7f.1"),
    ("loopback shortened", "127.1"),
    ("loopback shortened further", "127.0.1"),
    ("the metadata address as one decimal number", "2852039166"),
    ("a public address as one decimal number", "1568399394"),
    ("loopback in IPv6", "::1"),
    ("loopback in IPv6 with brackets", "[::1]"),
    ("an IPv6 unique-local address", "fd00::1"),
    ("an IPv6 link-local address", "fe80::1"),
    ("a public IPv6 address", "2001:db8::1"),
    ("a name with spaces around it", "  localhost  "),
    ("nothing at all", ""),
    ("a name that is only a dot", "."),
    ("a number that is not an address", "999999999999"),
]

PATTERNS: list[tuple[str, str | None]] = [
    ("no pattern at all", None),
    ("an ordinary pattern", r"error: \d+"),
    ("an empty pattern", ""),
    ("a nested quantifier", "(a+)+"),
    ("a quantified backreference", r"(a)\1+"),
    ("a pattern at the length cap", "a" * 512),
    ("a pattern over the length cap", "a" * 513),
    ("a pattern that will not compile", "(unclosed"),
    ("a pattern with a lone bracket", "[a-"),
]

IDS: list[tuple[str, str]] = [
    ("an ordinary id", "sess-1"),
    ("an id with a slash in it", "../etc/passwd"),
    ("an id with a dot segment", ".."),
    ("an empty id", ""),
    ("an id with a null byte", "a\x00b"),
    ("an id with a space", "a b"),
    ("an id of digits", "12345"),
    ("an id with dots", "a.b"),
]

CONNECTORS = ["shell", "telnet", "ssh", "ws", "websocket", "pty", "ushell", "vnc", "", "SHELL"]


# What ``inet_aton`` accepts, recorded directly: the guard's whole point is
# that a resolver takes forms a dotted-quad blocklist does not, and guessing
# which ones would be exactly the mistake being guarded against.
INET_ATON: list[str] = [
    "127.0.0.1",
    "1.2.3.4",
    "0.0.0.0",
    "255.255.255.255",
    "256.0.0.1",
    "1.2.3.4.5",
    "1.2.3.4.0",
    "1.2.3.4.5.6",
    "1.2.3.",
    ".1.2.3",
    "1..2.3",
    "",
    " 1.2.3.4",
    "1.2.3.4 ",
    "1.2.3.4\n",
    "127.1",
    "127.0.1",
    "2130706433",
    "4294967295",
    "4294967296",
    "0177.0.0.1",
    "0x7f.0.0.1",
    "0x7f000001",
    "0x7f.1",
    "0377.0377.0377.0377",
    "0400.0.0.1",
    "010",
    "0x",
    "0xg",
    "-1",
    "+1",
    "1e3",
    "1.2.3.0x4",
    "localhost",
    "999999999999",
    "1.2.3.4/24",
    "1.16777215",
    "1.16777216",
    "1.2.65535",
    "1.2.65536",
    "1.2.3.255",
    "1.2.3.256",
    "0",
    "1.2.3.4\t",
    "1.2.3.4x",
    "0xffffffff",
    "0x100000000",
]


def _aton(value: str) -> str | None:
    """The dotted quad ``inet_aton`` makes of this, or nothing when it refuses."""
    import socket

    try:
        packed = socket.inet_aton(value)
    except OSError:
        return None
    return str(__import__("ipaddress").IPv4Address(packed))


def _host(value: str) -> bool:
    return server_validators._is_internal_host(value)


def _pattern(value: str | None) -> dict[str, Any] | None:
    return server_validators._reject_bad_pattern(value)


def _id(value: str) -> dict[str, Any] | None:
    return server_validators._reject_bad_id(value, "session_id")


def _role(tool: str) -> dict[str, Any]:
    try:
        return {"role": policy.required_role(tool), "error": None}
    except KeyError as exc:
        return {"role": None, "error": str(exc.args[0])}


def main() -> None:
    corpus = {
        "roles": {role: policy.role_rank(role) for role in ROLES},
        "role_ladder": [
            {"actual": actual, "minimum": minimum, "allowed": policy.role_at_least(actual, minimum)}
            for actual in ROLES
            for minimum in ("viewer", "operator", "admin")
        ],
        "tool_roles": dict(policy.TOOL_REQUIRED_ROLES),
        "unknown_tool": _role("session_delete"),
        "hijack_lease_required": sorted(policy.HIJACK_LEASE_REQUIRED_TOOLS),
        "allowed_connectors": sorted(policy.ALLOWED_CONNECTOR_TYPES),
        "connectors": {name: policy.is_allowed_connector(name) for name in CONNECTORS},
        "max_user_pattern_len": constants.MAX_USER_PATTERN_LEN,
        "max_keystroke_bytes": constants.MAX_KEYSTROKE_BYTES,
        "allow_private_hosts": constants.ALLOW_PRIVATE_HOSTS,
        "inet_aton": [{"value": value, "address": _aton(value)} for value in INET_ATON],
        "hosts": [{"name": name, "host": host, "internal": _host(host)} for name, host in HOSTS],
        "patterns": [{"name": name, "pattern": value, "rejection": _pattern(value)} for name, value in PATTERNS],
        "ids": [{"name": name, "id": value, "rejection": _id(value)} for name, value in IDS],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['tool_roles'])} tools, {len(corpus['hosts'])} hosts)")


if __name__ == "__main__":
    main()
