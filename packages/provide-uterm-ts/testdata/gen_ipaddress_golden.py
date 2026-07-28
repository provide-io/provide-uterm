#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript `ipaddress` shim.

Address parsing is a security boundary in this codebase, not a formatting
detail. It decides whether an SSH server may accept any credential (loopback
bind only), whether a proxy-supplied header is trusted, and whether an
outbound request is allowed to reach a link-local metadata service. Every one
of those fails *open* if the parser is more permissive than CPython's.

The interesting cases are the ones that look like an address and are not:
``0177.0.0.1`` (CPython rejects leading zeros outright, older parsers read it
as octal), ``2130706433`` (the integer form ``inet_aton`` accepts), a trailing
dot, and ``::ffff:127.0.0.1``, which *is* loopback. A shim that guesses at any
of these turns a refusal into an acceptance.

The corpus is recorded from CPython's own `ipaddress`, so what is pinned is
that dialect rather than a reading of the RFCs.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_ipaddress_golden.py
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("ipaddress_golden.json")

# Everything the codebase's callers may be handed, valid or not.
CASES: list[str] = [
    # IPv4, plain.
    "0.0.0.0",  # the unspecified address is a case under test
    "127.0.0.1",
    "127.0.0.2",
    "127.255.255.254",
    "126.255.255.255",
    "128.0.0.1",
    "10.0.0.1",
    "172.15.255.255",
    "172.16.0.1",
    "172.31.255.255",
    "172.32.0.1",
    "192.168.1.10",
    "192.167.255.255",
    "169.254.169.254",
    "169.253.255.255",
    "100.100.100.200",
    "224.0.0.1",
    "239.255.255.255",
    "240.0.0.1",
    "255.255.255.255",
    "8.8.8.8",
    # IPv4, malformed in ways that used to parse.
    "0177.0.0.1",
    "010.0.0.1",
    "2130706433",
    "127.0.0.1.",
    ".127.0.0.1",
    "127.0.0",
    "127.0.0.1.1",
    "127.0.0.256",
    "127.0.0.-1",
    "127.0.0.1 ",
    " 127.0.0.1",
    "127.0.0.1/8",
    # Confusable digits, deliberately: a Unicode-aware parser reads these as
    # digits and CPython does not, so the ambiguity is the case under test.
    "12７.0.0.1",  # noqa: RUF001  # FULLWIDTH DIGIT SEVEN
    "127.0.0.٠",  # noqa: RUF001  # ARABIC-INDIC DIGIT ZERO
    # IPv6.
    "::",
    "::1",
    "0:0:0:0:0:0:0:1",
    "fe80::1",
    "febf::1",
    "fec0::1",
    "fd00:ec2::254",
    "fc00::1",
    "fbff::1",
    "ff02::1",
    "2001:db8::1",
    "64:ff9b::7f00:1",
    "::ffff:127.0.0.1",
    "::ffff:8.8.8.8",
    "::ffff:0:127.0.0.1",
    # 6to4 and the deprecated IPv4-compatible form, both of which carry a
    # reachable IPv4 that a membership check has to see through.
    "2002:a9fe:a9fe::1",
    "2002:0102:0304::",
    "::169.254.169.254",
    "::1.2.3.4",
    "64:ff9b::169.254.169.254",
    # The private-list exceptions, which are carved back out of a block that
    # is otherwise private.
    "192.0.0.8",
    "192.0.0.9",
    "192.0.0.10",
    "192.0.0.11",
    "2001:1::1",
    "2001:1::2",
    "2001:1::3",
    # Compression: one zero hextet is written out, and of two runs of equal
    # length the leftmost is the one elided.
    "1:2:3:4:5:6:0:8",
    "1:0:0:2:0:0:3:4",
    "0:0:0:0:0:0:0:0",
    # IPv6, malformed.
    "1:2:3:4:5:6:7::8",
    "1:2:3",
    "::127.0.0.1:ffff",
    # A malformed IPv4 tail. The octal bypass has to be refused here too, or
    # it comes back through the IPv6 door.
    "::1.2.3",
    "::256.0.0.1",
    "::0177.0.0.1",
    "fe80::1%",
    "[::1]",
    ":::1",
    "::1::",
    "12345::",
    "fe80::1%eth0",
    "fe80::1%0",
    "",
    "localhost",
    "example.org",
    "not an address",
]

# (name, network, members) — membership, which the egress guard turns into an
# allow or a refusal.
NETWORK_CASES: list[tuple[str, str, list[str]]] = [
    (
        "the NAT64 well-known prefix",
        "64:ff9b::/96",
        ["64:ff9b::7f00:1", "64:ff9b::1", "64:ff9b:1::1", "2001:db8::1", "::1"],
    ),
    ("a v4 host route", "127.0.0.1/32", ["127.0.0.1", "127.0.0.2"]),
    ("the v4 loopback block", "127.0.0.0/8", ["127.0.0.1", "127.255.255.254", "128.0.0.1"]),
    ("a v4 network across versions", "10.0.0.0/8", ["10.1.2.3", "::1", "11.0.0.1"]),
    # The bits of `::` and `0.0.0.0` are identical as far as a prefix compare
    # can see, so this is the case a missing version check gets wrong.
    ("a v4 network against v6 members", "0.0.0.0/8", ["0.0.0.1", "::", "::1"]),
]


def _describe(text: str) -> dict[str, Any]:
    """Parse `text` and record what CPython makes of it."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return {"input": text, "valid": False}
    return {
        "input": text,
        "valid": True,
        "version": address.version,
        # The normalised form, which is what a comparison or a log should use.
        "normalized": str(address),
        "packed": list(address.packed),
        "loopback": address.is_loopback,
        "private": address.is_private,
        "link_local": address.is_link_local,
        "multicast": address.is_multicast,
        "reserved": address.is_reserved,
        "unspecified": address.is_unspecified,
        # The embedded-IPv4 forms. A wrapper that hides a metadata address
        # from a membership check is the whole reason these are read.
        "ipv4_mapped": str(address.ipv4_mapped) if getattr(address, "ipv4_mapped", None) else None,
        "sixtofour": str(address.sixtofour) if getattr(address, "sixtofour", None) else None,
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "addresses": [_describe(text) for text in CASES],
        "networks": [
            {
                "name": name,
                "network": cidr,
                "normalized": str(ipaddress.ip_network(cidr)),
                "members": [
                    {"address": member, "contains": ipaddress.ip_address(member) in ipaddress.ip_network(cidr)}
                    for member in members
                ],
            }
            for name, cidr, members in NETWORK_CASES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    valid = sum(1 for row in corpus["addresses"] if row["valid"])
    print(f"wrote {OUT} ({valid} valid of {len(CASES)} addresses, {len(NETWORK_CASES)} networks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
