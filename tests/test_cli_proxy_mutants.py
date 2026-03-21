#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted mutation-killing tests for cli.py.

Tests for _cmd_proxy: args, app config, and uvicorn arguments.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from provide.terminal.cli import main

pytestmark = pytest.mark.timeout(10)


class TestCmdProxyArgs:
    """Kill mutants that replace args.host/args.bbs_port with None."""

    def test_proxy_passes_host_to_terminal_proxy(self) -> None:
        """mutmut_55: args.host must not be replaced with None."""
        captured: list[tuple] = []

        class _CapturingProxy:
            def __init__(self, host, port, *, transport_factory=None):
                captured.append((host, port))

            def create_router(self, path):
                from fastapi import APIRouter

                return APIRouter()

        mock_uvicorn = MagicMock()
        with (
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
            patch("provide.terminal.fastapi.WsTerminalProxy", _CapturingProxy),
        ):
            main(["proxy", "bbs.example.com", "23"])

        assert len(captured) == 1
        assert captured[0][0] == "bbs.example.com"
        assert captured[0][0] is not None

    def test_proxy_passes_bbs_port_to_terminal_proxy(self) -> None:
        """mutmut_56: args.bbs_port must not be replaced with None."""
        captured: list[tuple] = []

        class _CapturingProxy:
            def __init__(self, host, port, *, transport_factory=None):
                captured.append((host, port))

            def create_router(self, path):
                from fastapi import APIRouter

                return APIRouter()

        mock_uvicorn = MagicMock()
        with (
            patch.dict("sys.modules", {"uvicorn": mock_uvicorn}),
            patch("provide.terminal.fastapi.WsTerminalProxy", _CapturingProxy),
        ):
            main(["proxy", "bbs.example.com", "9999"])

        assert len(captured) == 1
        assert captured[0][1] == 9999
        assert captured[0][1] is not None

    def test_proxy_title_contains_host_and_port(self) -> None:
        """mutmut_55/56: page title uses args.host:args.bbs_port."""
        from starlette.testclient import TestClient

        captured_app: dict = {}
        mock_uv_mod = MagicMock()
        mock_uv_mod.run = MagicMock(side_effect=lambda app, **kw: captured_app.update({"app": app}))

        with patch.dict("sys.modules", {"uvicorn": mock_uv_mod}):
            main(["proxy", "my.host.com", "1234"])

        client = TestClient(captured_app["app"])
        resp = client.get("/")
        assert resp.status_code == 200
        assert "my.host.com" in resp.text
        assert "1234" in resp.text


# ---------------------------------------------------------------------------
# _cmd_proxy — mutmut_63/64/65/66/67
# FastAPI app title, docs_url, redoc_url
# ---------------------------------------------------------------------------


class TestCmdProxyAppConfig:
    """Kill mutants that change FastAPI app config."""

    def _get_app(self, path: str = "/ws/term") -> object:
        captured_app: dict = {}
        mock_uv_mod = MagicMock()
        mock_uv_mod.run = MagicMock(side_effect=lambda app, **kw: captured_app.update({"app": app}))

        with patch.dict("sys.modules", {"uvicorn": mock_uv_mod}):
            main(["proxy", "host", "23", "--path", path])
        return captured_app["app"]

    def test_docs_url_is_disabled(self) -> None:
        """mutmut_64: docs_url=None must be set (docs endpoint disabled)."""
        from starlette.testclient import TestClient

        app = self._get_app()
        client = TestClient(app)
        resp = client.get("/docs")
        # docs disabled → 404
        assert resp.status_code == 404

    def test_redoc_url_is_disabled(self) -> None:
        """mutmut_65: redoc_url=None must be set (redoc endpoint disabled)."""
        from starlette.testclient import TestClient

        app = self._get_app()
        client = TestClient(app)
        resp = client.get("/redoc")
        # redoc disabled → 404
        assert resp.status_code == 404

    def test_static_mount_at_slash_static(self) -> None:
        """mutmut_78: static mount path must be /static not /STATIC."""
        app = self._get_app()
        # Check that the mount is named "frontend" and at /static path
        static_routes = [r for r in app.routes if getattr(r, "name", None) == "frontend"]
        assert len(static_routes) >= 1
        # The mount path should be /static (not /STATIC)
        mount = static_routes[0]
        assert mount.path == "/static"


# ---------------------------------------------------------------------------
# _cmd_proxy — mutmut_88/92/93/94
# uvicorn.run log_level argument
# ---------------------------------------------------------------------------


class TestCmdProxyUvicornArgs:
    """Kill mutants that change uvicorn.run arguments."""

    def test_uvicorn_log_level_is_warning(self) -> None:
        """mutmut_88/92/93/94: log_level must be 'warning'."""
        mock_uvicorn = MagicMock()
        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
            main(["proxy", "bbs.example.com", "23"])

        call_kwargs = mock_uvicorn.run.call_args
        assert call_kwargs.kwargs.get("log_level") == "warning"

    def test_uvicorn_receives_log_level(self) -> None:
        """mutmut_92: log_level kwarg must be present."""
        mock_uvicorn = MagicMock()
        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}):
            main(["proxy", "bbs.example.com", "23"])

        call_kwargs = mock_uvicorn.run.call_args
        assert "log_level" in call_kwargs.kwargs


# ---------------------------------------------------------------------------
# _cmd_listen — mutmut_6: sys.exit(2) instead of sys.exit(1)
# mutmut_9: and → or for port check
# ---------------------------------------------------------------------------
