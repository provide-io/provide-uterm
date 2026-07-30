#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the egress IP-classification golden by invoking the REAL server
egress guard (provide.uterm.server.egress). Run from the repo root:

    uv run python packages/provide-uterm-go/server/testdata/gen_egress_golden.py

Writes egress_golden.json next to this script. The Go differential test
(server_egress_golden_test.go) asserts the Go classifier reproduces every
verdict here, so a drift in either implementation fails CI.
"""

from __future__ import annotations

import asyncio
import json
import pathlib

from provide.uterm.server.egress import (
    EgressBlockedError,
    assert_ip_allowed,
    assert_webhook_target_allowed,
)

# Literal IPs spanning: cloud metadata (always blocked), public v4/v6 (allowed),
# every private/loopback/link-local/multicast/reserved/unspecified v4 range,
# v6 specials, and every embedded-IPv4 IPv6 form (mapped / 6to4 / NAT64 / compat).
IPS = [
    "169.254.169.254",
    "100.100.100.200",
    "fd00:ec2::254",  # metadata
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",  # public v4
    "10.0.0.1",
    "10.255.255.255",
    "172.16.5.5",
    "172.31.0.1",
    "192.168.1.1",
    "127.0.0.1",
    "0.0.0.0",
    "255.255.255.255",
    "169.254.1.1",
    # RFC 6598 carrier-grade NAT (100.64.0.0/10). CPython does NOT call this
    # private, so the classifier-derived deny list needs the range named
    # explicitly. Boundaries either side are recorded too, so the mask itself is
    # pinned rather than the single address.
    "100.64.0.0",
    "100.64.0.1",
    "100.127.255.255",
    "100.63.255.255",  # public, immediately below the /10
    "100.128.0.0",  # public, immediately above the /10
    "224.0.0.1",
    "239.1.2.3",
    "240.0.0.1",
    "198.18.0.1",
    "192.0.2.1",
    "203.0.113.9",
    "192.0.0.170",
    "198.51.100.7",
    "2606:4700:4700::1111",
    "2600:1901::1",  # public v6
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd12::34",
    "ff02::1",
    "2001:db8::1",
    "2001::1",
    "64:ff9b:1::1",
    "::ffff:169.254.169.254",
    "::ffff:8.8.8.8",
    "::ffff:10.0.0.1",  # v4-mapped
    "2002:a9fe:a9fe::",
    "2002:0808:0808::",  # 6to4
    "64:ff9b::169.254.169.254",
    "64:ff9b::8.8.8.8",  # NAT64
    "::a9fe:a9fe",
    "::0808:0808",  # v4-compat
]

# Webhook targets: only cloud-metadata literal hosts are blocked; private and
# public hosts are allowed (webhook guard blocks metadata only).
WEBHOOK_URLS = [
    "https://169.254.169.254/health",
    "https://[fd00:ec2::254]:8443/x",
    "http://100.100.100.200/",
    "https://10.0.0.5/policy",
    "https://8.8.8.8/hook",
    "https://[64:ff9b::169.254.169.254]/x",  # NAT64-wrapped metadata
    "not a url at all",
    "",
]


def ip_row(ip: str) -> dict:
    def blocked(bp: bool) -> tuple[bool, str]:
        try:
            assert_ip_allowed(ip, block_private=bp)
            return False, ""
        except EgressBlockedError as e:
            return True, "metadata" if "metadata" in str(e) else "private"

    meta_blocked, meta_reason = blocked(False)
    priv_blocked, priv_reason = blocked(True)
    return {
        "ip": ip,
        "blocked_default": meta_blocked,  # block_private=False
        "blocked_default_reason": meta_reason,
        "blocked_private": priv_blocked,  # block_private=True
        "blocked_private_reason": priv_reason,
    }


async def webhook_row(url: str) -> dict:
    try:
        await assert_webhook_target_allowed(url)
        return {"url": url, "blocked": False}
    except EgressBlockedError:
        return {"url": url, "blocked": True}


async def main() -> None:
    rows = [ip_row(ip) for ip in IPS]
    webhooks = [await webhook_row(u) for u in WEBHOOK_URLS]
    out = {"ips": rows, "webhooks": webhooks}
    dest = pathlib.Path(__file__).with_name("egress_golden.json")
    dest.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {dest} ({len(rows)} ips, {len(webhooks)} webhooks)")


if __name__ == "__main__":
    asyncio.run(main())
