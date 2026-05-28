#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

from __future__ import annotations

import importlib
import sys

import pytest


def test_client_package_defers_hijack_import() -> None:
    sys.modules.pop("provide.uterm.client", None)
    hijack_preloaded = "provide.uterm.client.hijack" in sys.modules

    mod = importlib.import_module("provide.uterm.client")
    if not hijack_preloaded:
        assert "provide.uterm.client.hijack" not in sys.modules

    _ = mod.HijackClient
    assert "provide.uterm.client.hijack" in sys.modules
    _ = mod.SyncInlineWebSocketClient
    with pytest.raises(AttributeError):
        _ = mod.__not_exported__


def test_transports_package_defers_optional_transports() -> None:
    sys.modules.pop("provide.uterm.transports", None)
    ssh_preloaded = "provide.uterm.transports.ssh" in sys.modules
    telnet_preloaded = "provide.uterm.transports.telnet" in sys.modules
    ws_preloaded = "provide.uterm.transports.websocket" in sys.modules

    mod = importlib.import_module("provide.uterm.transports")
    if not ssh_preloaded:
        assert "provide.uterm.transports.ssh" not in sys.modules
    if not telnet_preloaded:
        assert "provide.uterm.transports.telnet" not in sys.modules
    if not ws_preloaded:
        assert "provide.uterm.transports.websocket" not in sys.modules

    _ = mod.start_ssh_server
    assert "provide.uterm.transports.ssh" in sys.modules
    with pytest.raises(AttributeError):
        _ = mod.__not_exported__
