#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from provide.uterm.bridge.schemas import HelloFrame


def test_hello_capabilities_parsing():
    caps = HelloFrame(type="hello", hijack_control="ws", resume_supported=True, mcp_supported=True, vnc_supported=False)
    assert caps.mcp_supported is True
    assert caps.vnc_supported is False
