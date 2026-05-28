#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for uterm watch CLI + WatchApp data model."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from provide.uterm.cli import _build_parser
from provide.uterm.cli._watch_app import Exchange, _decode_body, human_size, parse_http_frames, status_style
from provide.uterm.cli.watch import extract_tunnel_id
from provide.uterm.control_channel import encode_control


class TestWatchArgParsing:
    def test_watch_subcommand_recognised(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["watch", "tunnel-abc123", "-s", "https://example.com"])
        assert args.tunnel == "tunnel-abc123"
        assert args.server == "https://example.com"

    def test_watch_requires_tunnel(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["watch", "-s", "https://example.com"])

    def test_watch_has_func(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["watch", "tunnel-abc", "-s", "https://x.com"])
        assert hasattr(args, "func")

    def test_watch_layout_choices(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["watch", "t-1", "-s", "https://x.com", "--layout", "vertical"])
        assert args.layout == "vertical"

    def test_watch_layout_invalid(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["watch", "t-1", "-s", "https://x.com", "--layout", "invalid"])

    def test_watch_default_layout(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["watch", "t-1", "-s", "https://x.com"])
        assert args.layout == "horizontal"


class TestExtractTunnelId:
    def test_bare_id(self) -> None:
        assert extract_tunnel_id("tunnel-abc123") == "tunnel-abc123"

    def test_url_with_inspect(self) -> None:
        assert extract_tunnel_id("https://worker.dev/app/inspect/tunnel-abc") == "tunnel-abc"

    def test_url_with_session(self) -> None:
        assert extract_tunnel_id("https://worker.dev/app/session/tunnel-abc") == "tunnel-abc"

    def test_url_with_short(self) -> None:
        assert extract_tunnel_id("https://worker.dev/s/tunnel-abc") == "tunnel-abc"

    def test_url_with_query(self) -> None:
        assert extract_tunnel_id("https://worker.dev/app/inspect/tunnel-abc?token=xyz") == "tunnel-abc"

    def test_plain_string(self) -> None:
        assert extract_tunnel_id("my-session-id") == "my-session-id"

    def test_url_with_operator(self) -> None:
        assert extract_tunnel_id("https://worker.dev/app/operator/tunnel-abc") == "tunnel-abc"


class TestParseHttpFrames:
    def test_extracts_http_req(self) -> None:
        raw = encode_control({"type": "http_req", "id": "r1", "_channel": "http", "method": "GET", "url": "/test"})
        frames = parse_http_frames(raw)
        assert len(frames) == 1
        assert frames[0]["method"] == "GET"

    def test_extracts_http_res(self) -> None:
        raw = encode_control({"type": "http_res", "id": "r1", "_channel": "http", "status": 200, "duration_ms": 42})
        frames = parse_http_frames(raw)
        assert len(frames) == 1
        assert frames[0]["status"] == 200

    def test_ignores_non_http(self) -> None:
        raw = encode_control({"type": "hello", "worker_id": "x"})
        frames = parse_http_frames(raw)
        assert len(frames) == 0

    def test_ignores_plain_data(self) -> None:
        assert parse_http_frames("plain terminal data") == []

    def test_multiple_frames(self) -> None:
        f1 = encode_control({"type": "http_req", "id": "r1", "_channel": "http", "method": "GET"})
        f2 = encode_control({"type": "http_res", "id": "r1", "_channel": "http", "status": 200})
        frames = parse_http_frames(f1 + f2)
        assert len(frames) == 2


class TestExchange:
    def test_create(self) -> None:
        ex = Exchange(req_id="r1", method="POST", url="/api/login", req_body_size=42)
        assert ex.method == "POST"
        assert ex.status is None

    def test_complete(self) -> None:
        ex = Exchange(req_id="r1", method="GET", url="/api")
        ex.status = 200
        ex.duration_ms = 89
        assert ex.status == 200


class TestHumanSize:
    def test_bytes(self) -> None:
        assert human_size(512) == "512B"

    def test_kb(self) -> None:
        assert human_size(3200) == "3.1KB"

    def test_mb(self) -> None:
        assert human_size(2 * 1024 * 1024) == "2.0MB"


class TestStatusStyle:
    def test_2xx(self) -> None:
        assert "green" in status_style(200)

    def test_3xx(self) -> None:
        assert "yellow" in status_style(301)

    def test_4xx(self) -> None:
        assert "yellow" in status_style(404)

    def test_5xx(self) -> None:
        assert "red" in status_style(500)

    def test_none(self) -> None:
        assert status_style(None) == "dim"


class TestDecodeBody:
    def test_b64(self) -> None:
        body = base64.b64encode(b'{"user":"admin"}').decode()
        assert _decode_body(body, False, False, 17) == '{"user":"admin"}'

    def test_truncated(self) -> None:
        assert "(truncated" in _decode_body(None, True, False, 300000)

    def test_binary(self) -> None:
        assert "(binary" in _decode_body(None, False, True, 1024)

    def test_empty(self) -> None:
        assert _decode_body(None, False, False, 0) == ""

    def test_invalid_b64(self) -> None:
        assert _decode_body("!!!not-valid-base64!!!", False, False, 10) == "(decode error)"


class TestParseHttpFramesEdge:
    def test_malformed_json_in_control(self) -> None:
        """Line 84-85: invalid JSON inside a valid control frame envelope."""
        raw = "\x10\x0200000010:not valid json"
        frames = parse_http_frames(raw)
        assert frames == []

    def test_dle_not_followed_by_stx(self) -> None:
        """Line 88: DLE followed by non-STX char (data chunk)."""
        raw = "\x10Xsome data"
        frames = parse_http_frames(raw)
        assert frames == []

    def test_dle_at_end(self) -> None:
        raw = "data\x10"
        frames = parse_http_frames(raw)
        assert frames == []


class TestReadToken:
    def test_explicit_token(self) -> None:
        from unittest.mock import MagicMock

        from provide.uterm.cli.watch import _read_token

        args = MagicMock(token="my-tok", token_file=None)
        assert _read_token(args) == "my-tok"

    def test_from_file(self, tmp_path: object) -> None:
        from pathlib import Path
        from unittest.mock import MagicMock

        from provide.uterm.cli.watch import _read_token

        token_file = Path(str(tmp_path)) / "token"
        token_file.write_text("file-token-123\n")
        args = MagicMock(token=None, token_file=str(token_file))
        assert _read_token(args) == "file-token-123"

    def test_no_token(self) -> None:
        from unittest.mock import MagicMock

        from provide.uterm.cli.watch import _read_token

        args = MagicMock(token=None, token_file="/nonexistent")
        assert _read_token(args) is None


class TestCmdWatch:
    def test_token_uses_authorization_header_not_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from provide.uterm.cli.watch import _cmd_watch

        captured: dict[str, object] = {}

        class _FakeWatchApp:
            def __init__(
                self,
                *,
                ws_url: str,
                tunnel_id: str,
                initial_layout: str,
                headers: dict[str, str] | None,
            ) -> None:
                captured["ws_url"] = ws_url
                captured["tunnel_id"] = tunnel_id
                captured["initial_layout"] = initial_layout
                captured["headers"] = headers

            def run(self) -> None:
                captured["ran"] = True

        monkeypatch.setattr("provide.uterm.cli._watch_app.WatchApp", _FakeWatchApp)
        args = SimpleNamespace(
            tunnel="tunnel-abc",
            server="https://example.test",
            layout="horizontal",
            token="secret-token",
            token_file=None,
        )

        _cmd_watch(args)

        assert captured["ws_url"] == "wss://example.test/ws/browser/tunnel-abc/term"
        assert captured["headers"] == {"Authorization": "Bearer secret-token"}
        assert captured["ran"] is True
