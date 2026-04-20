#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.terminal.gateway.__init__ — public API surface."""

from __future__ import annotations

from pathlib import Path

import provide.terminal.gateway as gateway
from provide.terminal.gateway._gateway import _delete_token, _read_token, _write_token


class TestPublicExports:
    def test_all_exports_exist(self) -> None:
        for name in gateway.__all__:
            assert hasattr(gateway, name), f"Missing export: {name}"

    def test_all_is_complete(self) -> None:
        expected = {
            "SshWsGateway",
            "TelnetWsGateway",
            "_delete_token",
            "_handle_ws_control",
            "_normalize_crlf",
            "_pipe_ws",
            "_read_token",
            "_ssh_to_ws",
            "_strip_iac",
            "_tcp_to_ws",
            "_write_token",
            "_ws_to_ssh",
            "_ws_to_tcp",
        }
        assert set(gateway.__all__) == expected

    def test_classes_importable(self) -> None:
        assert gateway.TelnetWsGateway is not None
        assert gateway.SshWsGateway is not None

    def test_functions_callable(self) -> None:
        assert callable(gateway._normalize_crlf)
        assert callable(gateway._strip_iac)
        assert callable(gateway._handle_ws_control)
        assert callable(gateway._pipe_ws)
        assert callable(gateway._tcp_to_ws)
        assert callable(gateway._ws_to_tcp)
        assert callable(gateway._ssh_to_ws)
        assert callable(gateway._ws_to_ssh)


class TestTokenHelpers:
    def test_write_and_read_token(self, tmp_path: Path) -> None:
        p = tmp_path / "sub" / "token.txt"
        _write_token(p, "mytoken")
        assert _read_token(p) == "mytoken"

    def test_read_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_token(tmp_path / "nonexistent.txt") is None

    def test_read_empty_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.txt"
        p.write_text("   ")
        assert _read_token(p) is None

    def test_delete_existing_token(self, tmp_path: Path) -> None:
        p = tmp_path / "token.txt"
        _write_token(p, "tok")
        _delete_token(p)
        assert not p.exists()

    def test_delete_missing_token_does_not_raise(self, tmp_path: Path) -> None:
        _delete_token(tmp_path / "nonexistent.txt")
