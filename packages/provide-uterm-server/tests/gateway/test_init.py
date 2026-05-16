#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for provide.uterm.gateway.__init__ — public API surface."""

from __future__ import annotations

from pathlib import Path

import provide.uterm.gateway as gateway
from provide.uterm.gateway._gateway import (
    _delete_token,
    _read_token,
    _write_token,
)
from provide.uterm.gateway._ssh_handler import _token_file_for_connection


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
    def test_write_and_read_token_round_trip(self, tmp_path: Path) -> None:
        """_write_token persists JSON {token, player_id}; _read_token returns the dict."""
        p = tmp_path / "sub" / "token.json"
        _write_token(p, "mytoken", player_id=42)
        record = _read_token(p)
        assert isinstance(record, dict)
        assert record["token"] == "mytoken"
        assert record["player_id"] == 42

    def test_write_token_sets_0600_perms(self, tmp_path: Path) -> None:
        """Token files are a bearer credential → must be owner-read-only."""
        import stat as _stat

        p = tmp_path / "token.json"
        _write_token(p, "t")
        assert _stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_read_legacy_bare_token_normalises(self, tmp_path: Path) -> None:
        """Older token files were a bare token string — keep them working."""
        p = tmp_path / "legacy.txt"
        p.write_text("bare-legacy-token")
        record = _read_token(p)
        assert isinstance(record, dict)
        assert record["token"] == "bare-legacy-token"
        assert "player_id" not in record

    def test_read_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_token(tmp_path / "nonexistent.txt") is None

    def test_read_empty_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.txt"
        p.write_text("   ")
        assert _read_token(p) is None

    def test_delete_existing_token(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        _write_token(p, "tok")
        _delete_token(p)
        assert not p.exists()

    def test_delete_missing_token_does_not_raise(self, tmp_path: Path) -> None:
        _delete_token(tmp_path / "nonexistent.txt")


class TestTokenFileForConnection:
    """Per-connection token-path resolver — used by SSH gateway to pick a
    fingerprint-scoped token file when the caller passes a directory."""

    def test_none_base_returns_none(self, tmp_path: Path) -> None:
        assert _token_file_for_connection(None, "SHA256:abc") is None

    def test_file_path_returned_verbatim(self, tmp_path: Path) -> None:
        base = tmp_path / "token.json"
        base.write_text("{}")
        assert _token_file_for_connection(base, "SHA256:abc") == base

    def test_directory_plus_fingerprint_slugs_into_filename(self, tmp_path: Path) -> None:
        base = tmp_path  # existing directory
        out = _token_file_for_connection(base, "SHA256:aB+c/D:e")
        assert out is not None
        assert out.parent == base
        # Path-hostile characters are replaced.
        assert ":" not in out.name
        assert "/" not in out.name
        assert "+" not in out.name
        assert out.suffix == ".token"

    def test_dir_shaped_nonexistent_path_is_treated_as_directory(self, tmp_path: Path) -> None:
        """Bare `--persist-token-dir /tmp/warp-tokens` (dir doesn't exist yet)."""
        base = tmp_path / "tokens"
        out = _token_file_for_connection(base, "SHA256:abc")
        assert out is not None
        assert out.parent == base
        assert out.suffix == ".token"

    def test_directory_without_fingerprint_falls_back_to_base(self, tmp_path: Path) -> None:
        """Client with no offered pubkey → proxy-wide token at the dir root."""
        base = tmp_path
        assert _token_file_for_connection(base, None) == base
