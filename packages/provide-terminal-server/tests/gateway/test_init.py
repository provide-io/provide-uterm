#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway.__init__ — public API surface."""

from __future__ import annotations

import provide.terminal.gateway as gateway


class TestPublicExports:
    def test_all_exports_exist(self) -> None:
        for name in gateway.__all__:
            assert hasattr(gateway, name), f"Missing export: {name}"

    def test_all_is_complete(self) -> None:
        expected = {
            "SshWsGateway",
            "TelnetWsGateway",
            "_apply_color_mode",
            "_clamp8",
            "_handle_ws_control",
            "_normalize_crlf",
            "_pipe_ws",
            "_rgb_to_16_index",
            "_rgb_to_256",
            "_ssh_to_ws",
            "_strip_iac",
            "_tcp_to_ws",
            "_ws_to_ssh",
            "_ws_to_tcp",
        }
        assert set(gateway.__all__) == expected

    def test_classes_importable(self) -> None:
        assert gateway.TelnetWsGateway is not None
        assert gateway.SshWsGateway is not None

    def test_functions_callable(self) -> None:
        assert callable(gateway._apply_color_mode)
        assert callable(gateway._clamp8)
        assert callable(gateway._rgb_to_256)
        assert callable(gateway._rgb_to_16_index)
        assert callable(gateway._normalize_crlf)
        assert callable(gateway._strip_iac)
        assert callable(gateway._handle_ws_control)
        assert callable(gateway._pipe_ws)
        assert callable(gateway._tcp_to_ws)
        assert callable(gateway._ws_to_tcp)
        assert callable(gateway._ssh_to_ws)
        assert callable(gateway._ws_to_ssh)
