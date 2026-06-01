#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Guard-rail test: the M3 peer-IP mitigation must add NO TLS/SSH verification weakening.

The post-connect peer-IP validation closes the DNS-rebinding window by checking
the IP we actually reached.  It must never tamper with the handshake — TLS SNI,
TLS certificate validation, and SSH host-key/known-hosts verification all stay
intact (the handshake still uses the original hostname).  This test fails if a
verification-weakening token is introduced into the SSH or WebSocket connector.
"""

from __future__ import annotations

from pathlib import Path

_CONNECTORS = Path(__file__).resolve().parents[2] / "src" / "provide" / "uterm" / "server" / "connectors"

# Tokens that would disable TLS cert/SNI or SSH host-key verification.  None of
# these may appear in the connector source — the mitigation only reads the peer
# IP, it never changes verification.
_FORBIDDEN = (
    "check_hostname=False",
    "check_hostname = False",
    "CERT_NONE",
    "verify_mode",
    "known_hosts=None",
    "known_hosts = None",
    "server_host_key_algs",
    "insecure",
)


def test_no_verification_weakening_in_ssh_connector() -> None:
    src = (_CONNECTORS / "ssh.py").read_text()
    # The SSH connector legitimately references `known_hosts` and
    # `insecure_no_host_check` (a pre-existing, explicit opt-out config key) —
    # those are NOT introduced by this mitigation.  Assert the mitigation added
    # none of the hard verification-bypass tokens.
    for token in ("check_hostname=False", "CERT_NONE", "verify_mode", "known_hosts=None", "server_host_key_algs"):
        assert token not in src, f"forbidden verification-weakening token in ssh.py: {token!r}"


def test_no_verification_weakening_in_websocket_connector() -> None:
    src = (_CONNECTORS / "websocket.py").read_text()
    for token in _FORBIDDEN:
        assert token not in src, f"forbidden verification-weakening token in websocket.py: {token!r}"
