#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript egress guard.

This decides where a hosted session is allowed to connect out to, so every
case it gets wrong is reachable by whoever can create a session.

* **Cloud-metadata addresses are always blocked.** They are never a legitimate
  terminal target, and reaching one hands out cloud credentials.
* **A wrapper is not a disguise.** An IPv6 address can carry a reachable IPv4
  four different ways — mapped, 6to4, NAT64 and the deprecated compatible form
  — and each of them is a way past a membership check that only looks at the
  outer address. In a NAT64 cluster ``64:ff9b::169.254.169.254`` really does
  reach the v4 metadata service.
* **A name that will not resolve fails closed.** Otherwise a hostile or merely
  broken resolver turns the guard off.
* **Private ranges are blocked only when asked.** Reaching internal hosts is
  what a connector is *for*; the flag is the multi-tenant posture.

The corpus is recorded by driving the real guards with the resolver stubbed,
so the classification and the exact messages are the reference's.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_egress_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.server import egress as egress_module

OUT = Path(__file__).with_name("egress_golden.json")

# Every address the guard has to classify, literal.
LITERAL_CASES: list[str] = [
    "8.8.8.8",
    "203.0.113.10",
    # The metadata services, and their wrappers.
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",
    "::ffff:169.254.169.254",
    "2002:a9fe:a9fe::",
    "64:ff9b::169.254.169.254",
    "::169.254.169.254",
    "[169.254.169.254]",
    " 169.254.169.254 ",
    # Internal ranges, blocked only when asked.
    "127.0.0.1",
    "::1",
    "10.0.0.1",
    "192.168.1.10",
    "172.16.0.1",
    "169.254.1.1",
    "224.0.0.1",
    "240.0.0.1",
    "0.0.0.0",  # the unspecified address
    # RFC 6598 carrier-grade NAT. CPython does NOT call this private, so a
    # deny list derived from its classifiers has a hole here unless the range
    # is named. Recorded with an address either side of the /10 so a port that
    # guesses the mask — 100.64.0.0/16, or the whole of 100.0.0.0/8 — is caught
    # over-blocking rather than merely under-blocking.
    "100.64.0.0",
    "100.64.0.1",
    "100.127.255.255",
    "100.63.255.255",  # public, immediately below the /10
    "100.128.0.0",  # public, immediately above the /10
    "::",
    "fe80::1",
    "fc00::1",
    # Reserved but *not* private, so the reserved check is the only thing
    # standing between these and a connector.
    "100:0:0:1::1",
    "200::1",
    "1000::1",
    # Multicast but not private, likewise.
    "ff02::1",
    # Wrapped internal addresses.
    "::ffff:127.0.0.1",
    "2002:7f00:1::",
    "64:ff9b::127.0.0.1",
    "::127.0.0.1",
    # The IPv4-compatible form excludes :: and ::1, which the IPv6 branches
    # already cover.
    "::0.0.0.1",
]

# (name, host, resolved) — a name resolving to one or more addresses.
RESOLVE_CASES: list[tuple[str, str, list[str] | None]] = [
    ("a public name", "example.org", ["93.184.216.34"]),
    ("the webhook host", "policy.example.org", ["93.184.216.34"]),
    ("a name that resolves to metadata", "metadata.example", ["169.254.169.254"]),
    ("one bad address among good ones", "mixed.example", ["93.184.216.34", "169.254.169.254"]),
    ("a name that resolves to an internal address", "internal.example", ["10.0.0.1"]),
    ("a name that resolves to a wrapped metadata address", "wrapped.example", ["64:ff9b::169.254.169.254"]),
    ("a name that resolves to nothing", "empty.example", []),
    ("a name that will not resolve", "broken.example", None),
]

# (name, url) — webhook targets.
WEBHOOK_CASES: list[tuple[str, str]] = [
    ("a public https url", "https://policy.example.org/decide"),
    ("a metadata literal", "http://169.254.169.254/latest/meta-data/"),
    ("a bracketed ipv6 literal", "http://[fd00:ec2::254]/token"),
    ("an internal literal, which is allowed", "https://10.0.0.1/decide"),
    ("a wrapped metadata literal", "http://[64:ff9b::169.254.169.254]/token"),
    ("a url with no host at all", "file:///etc/passwd"),
    ("a name that resolves to metadata", "https://metadata.example/decide"),
    ("a name that resolves to nothing", "https://empty.example/decide"),
    ("a name that will not resolve", "https://broken.example/decide"),
]


def _failure(call: Any) -> str | None:
    """Run `call` and return the block message, or None when it is allowed."""
    try:
        call()
    except egress_module.EgressBlockedError as exc:
        return str(exc)
    return None


def _capture(call: Any) -> BaseException:
    """Run `call` and return whatever it raised."""
    try:
        call()
    except BaseException as exc:  # naming what escapes is the point
        return exc
    raise AssertionError("expected a failure")


async def _afailure(coro: Any) -> str | None:
    """Await `coro` and return the block message, or None when it is allowed."""
    try:
        await coro
    except egress_module.EgressBlockedError as exc:
        return str(exc)
    return None


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    resolutions = {host: addresses for _name, host, addresses in RESOLVE_CASES}

    async def fake_resolve(host: str) -> tuple[str, ...]:
        addresses = resolutions.get(host)
        if addresses is None:
            raise OSError(f"cannot resolve {host}")
        return tuple(addresses)

    egress_module._resolve_host = fake_resolve  # type: ignore[assignment]
    egress_module._resolve_cache.clear()

    literals = []
    for text in LITERAL_CASES:
        literals.append(
            {
                "input": text,
                "permissive": _failure(lambda t=text: egress_module.assert_ip_allowed(t, block_private=False)),
                "strict": _failure(lambda t=text: egress_module.assert_ip_allowed(t, block_private=True)),
            }
        )

    targets = []
    for name, host, _addresses in RESOLVE_CASES:
        targets.append(
            {
                "name": name,
                "host": host,
                "permissive": await _afailure(egress_module.assert_connector_target_allowed(host, block_private=False)),
                "strict": await _afailure(egress_module.assert_connector_target_allowed(host, block_private=True)),
            }
        )
    # A literal handed to the target guard skips resolution entirely.
    for literal in ("169.254.169.254", "10.0.0.1", "8.8.8.8"):
        targets.append(
            {
                "name": f"the literal {literal}",
                "host": literal,
                "permissive": await _afailure(
                    egress_module.assert_connector_target_allowed(literal, block_private=False)
                ),
                "strict": await _afailure(egress_module.assert_connector_target_allowed(literal, block_private=True)),
            }
        )

    webhooks = []
    for name, url in WEBHOOK_CASES:
        egress_module._resolve_cache.clear()
        webhooks.append(
            {"name": name, "url": url, "blocked": await _afailure(egress_module.assert_webhook_target_allowed(url))}
        )

    # Inputs that are not addresses or URLs at all.
    peer_failure = _capture(lambda: egress_module.assert_ip_allowed("not-an-address", block_private=False))
    malformed = {
        "peer_error": type(peer_failure).__name__,
        "peer_is_block": isinstance(peer_failure, egress_module.EgressBlockedError),
        "webhook_no_scheme": await _afailure(egress_module.assert_webhook_target_allowed("not a url")),
        "webhook_empty": await _afailure(egress_module.assert_webhook_target_allowed("")),
    }

    corpus = {
        "malformed": malformed,
        "literals": literals,
        "targets": targets,
        "webhooks": webhooks,
        "metadata_ips": sorted(str(ip) for ip in egress_module._METADATA_IPS),
        "nat64_prefix": str(egress_module._NAT64_WELL_KNOWN),
        "dns_ttl_s": egress_module._EGRESS_DNS_TTL_S,
        "resolve_timeout_s": egress_module._EGRESS_RESOLVE_TIMEOUT_S,
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(literals)} literals, {len(targets)} targets, {len(webhooks)} webhooks)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
