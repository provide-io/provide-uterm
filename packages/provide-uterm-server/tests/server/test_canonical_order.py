#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Convention test: connector registry follows canonical transport ordering.

Canonical order: network modern→legacy (websocket, ssh, telnet) then local (shell).
Dict/set lookup is order-independent, so reordering has no behavior effect;
this test guards the readability convention.
"""

from provide.uterm.server.connectors import registry

_NETWORK_CANON = ["websocket", "ssh", "telnet"]  # ws-slot first, telnet last
_LOCAL = {"shell"}


def test_builtin_connectors_follow_canonical_order() -> None:
    keys = list(registry._BUILTIN_CLASSES.keys())
    network = [k for k in keys if k not in _LOCAL]
    local = [k for k in keys if k in _LOCAL]
    assert network == _NETWORK_CANON, f"network connectors out of canonical order: {network}"
    # every local connector comes after every network one
    assert keys == network + local, f"local connectors must trail network: {keys}"
