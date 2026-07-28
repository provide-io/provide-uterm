#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-server
"""Generate the differential golden corpus for the server's security posture report.

Eight or so independent security knobs collapsed into one answer: which
weakening opt-outs are actually active, what the server is bound to, and a
single ``secure`` summary. Pure, so it can be logged at startup and served
from an auth-gated endpoint.

**An opt-out counts only where it is actually relaxing something.** Two of the
knobs are acknowledgements — they weaken nothing on their own and only matter
paired with the mode they unlock. Listing them unconditionally would report a
posture worse than the deployment has, and an operator who learns the report
cries wolf stops reading it.

**A relaxation on a loopback bind does not demote the posture.** A deployment
that cannot be reached off-box is not made insecure by relaxing a control on
it. So ``secure`` is production *and* (loopback *or* nothing relaxed).

**Two entries are warnings rather than opt-outs.** An unsigned webhook IdP and
a plain audit log do not relax an existing control — one is a gap in a
control's strength and the other a compliance note — so they are reported
without demoting the posture on their own.

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_posture_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.app.posture import compute_security_posture
from provide.uterm.server.config_schema import UtermServerConfig

OUT = Path(__file__).with_name("posture_golden.json")


def _config(**overrides: Any) -> Any:
    """A server configuration with a knob or two turned."""
    config = UtermServerConfig()
    for dotted, value in overrides.items():
        section, _, field = dotted.partition("__")
        target = config if field == "" else getattr(config, section)
        setattr(target, field or section, value)
    return config


# (name, overrides) — each posture worth reporting.
CASES: list[tuple[str, dict[str, Any]]] = [
    ("the defaults", {}),
    # A deployment with nothing relaxed at all, which is the only way a
    # routable production bind reports as secure.
    ("nothing relaxed, on loopback", {"auth__mode": "jwt", "security__block_private_connector_targets": True}),
    (
        "nothing relaxed, on a routable bind",
        {
            "environment": "production",
            "server__host": "0.0.0.0",
            "auth__mode": "jwt",
            "security__block_private_connector_targets": True,
        },
    ),
    (
        "one thing relaxed, on a routable bind",
        {
            "environment": "production",
            "server__host": "0.0.0.0",
            "auth__mode": "jwt",
            "security__block_private_connector_targets": True,
            "auth__allow_adhoc_browser_observers": True,
        },
    ),
    (
        "a development environment",
        {"environment": "development", "auth__mode": "jwt", "security__block_private_connector_targets": True},
    ),
    ("production on loopback", {"environment": "production"}),
    ("production on a routable bind", {"environment": "production", "server__host": "0.0.0.0"}),
    # The bind is read case- and space-insensitively, because it comes from a
    # config file a person wrote.
    ("a bind with surrounding space", {"server__host": " 127.0.0.1 "}),
    ("a bind in capitals", {"server__host": "LOCALHOST"}),
    ("an IPv6 loopback", {"server__host": "::1"}),
    ("a routable bind", {"server__host": "10.0.0.5"}),
    # Weakening opt-outs.
    ("dev token auth", {"auth__mode": "dev_token"}),
    ("dev security headers", {"security__mode": "dev"}),
    ("dev headers acknowledged", {"security__mode": "dev", "security__dev_mode_acknowledged": True}),
    # An acknowledgement on its own weakens nothing.
    ("headers acknowledged without dev mode", {"security__dev_mode_acknowledged": True}),
    ("header auth acknowledged", {"auth__mode": "header", "auth__header_mode_acknowledged": True}),
    ("header acknowledgement without header auth", {"auth__header_mode_acknowledged": True}),
    ("an anonymous viewer fallback", {"auth__webhook_idp_on_failure": "viewer"}),
    ("ad-hoc observers", {"auth__allow_adhoc_browser_observers": True}),
    ("connectors reaching internal hosts", {"security__block_private_connector_targets": False}),
    # Webhook IdP: signing and replay.
    ("a webhook idp, signed", {"auth__identity_provider": "webhook"}),
    (
        "a webhook idp, unsigned",
        {"auth__identity_provider": "webhook", "auth__webhook_idp_require_signed_response": False},
    ),
    (
        "a webhook idp with nonce binding",
        {"auth__identity_provider": "webhook", "auth__webhook_idp_require_response_nonce": True},
    ),
    # Unsigned matters only for the webhook provider.
    ("a local idp, unsigned", {"auth__webhook_idp_require_signed_response": False}),
    # Compliance.
    ("an audit chain", {"audit__chain_enabled": True}),
    # Production with a relaxation, on each kind of bind.
    (
        "production, routable, with an opt-out",
        {"environment": "production", "server__host": "0.0.0.0", "auth__allow_adhoc_browser_observers": True},
    ),
    (
        "production, loopback, with an opt-out",
        {"environment": "production", "auth__allow_adhoc_browser_observers": True},
    ),
    (
        "production, routable, with only a warning",
        {"environment": "production", "server__host": "0.0.0.0", "audit__chain_enabled": True},
    ),
    # Several at once, to pin the ordering of the list.
    (
        "everything relaxed at once",
        {
            "auth__mode": "dev_token",
            "security__mode": "dev",
            "security__dev_mode_acknowledged": True,
            "auth__webhook_idp_on_failure": "viewer",
            "auth__allow_adhoc_browser_observers": True,
            "security__block_private_connector_targets": False,
        },
    ),
]


def _build() -> dict[str, Any]:
    """What each configuration reports."""
    return {
        "postures": [
            {
                "name": name,
                "overrides": dict(overrides),
                "result": compute_security_posture(_config(**overrides)),
            }
            for name, overrides in CASES
        ],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CASES)} postures)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
