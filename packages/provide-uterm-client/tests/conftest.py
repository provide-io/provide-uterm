#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared pytest fixtures + auto-auth patches for provide-uterm-client tests.

Tests build server apps via ``create_server_app(... mode='header')`` and
hit them with TestClient/httpx/websockets. These patches inject the admin
header-mode credentials so tests don't have to attach them by hand.
"""

from __future__ import annotations

import asyncio
import asyncio.base_events
import contextlib
import socket
from typing import Any

import pytest


@pytest.fixture
def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _install_httpx_dev_principal_autoauth() -> None:
    """Attach admin header-mode credentials to test httpx clients."""
    import httpx

    if getattr(httpx.Client, "_uterm_devprincipal_patched", False):
        return

    _defaults = {"X-Uterm-Principal": "admin", "X-Uterm-Role": "admin"}

    def _patch(cls: type) -> None:
        _orig_init = cls.__init__

        def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            _orig_init(self, *args, **kwargs)
            for header, value in _defaults.items():
                if header not in self.headers:
                    self.headers[header] = value

        cls.__init__ = _patched_init  # type: ignore[method-assign]
        cls._uterm_devprincipal_patched = True  # type: ignore[attr-defined]

    _patch(httpx.Client)
    _patch(httpx.AsyncClient)


def _install_websockets_dev_principal_autoauth() -> None:
    """Attach admin/worker-bearer credentials to test websockets clients."""
    import websockets as _websockets
    import websockets.asyncio.client as _ws_client

    if getattr(_ws_client.connect, "_uterm_devprincipal_patched", False):
        return

    _defaults = {"X-Uterm-Principal": "admin", "X-Uterm-Role": "admin"}
    _worker_bearer = "Bearer test-bearer-token-32-chars-long-x"
    _orig = _ws_client.connect

    def _patched_connect(*args: Any, **kwargs: Any) -> Any:
        uri = args[0] if args else kwargs.get("uri", "")
        provided = kwargs.get("additional_headers") or {}
        merged = dict(_defaults)
        if isinstance(uri, str) and "/ws/worker/" in uri:
            merged["Authorization"] = _worker_bearer
        if isinstance(provided, dict):
            merged.update(provided)
        else:
            merged.update(dict(provided))
        kwargs["additional_headers"] = merged
        return _orig(*args, **kwargs)

    _patched_connect._uterm_devprincipal_patched = True  # type: ignore[attr-defined]
    _ws_client.connect = _patched_connect  # type: ignore[assignment]
    _websockets.connect = _patched_connect  # type: ignore[assignment]


def _install_bounded_server_wait_closed() -> None:
    """Bound ``asyncio.Server.wait_closed()`` so transport-test teardown can't hang.

    Python 3.12 changed ``Server.wait_closed()`` to block until every active
    connection closes. Many transport tests use a one-shot server whose handler
    holds the connection (writes, then sleeps/awaits) without closing its writer,
    so ``wait_closed()`` deadlocks there on 3.12 (3.11/3.13/3.14 do not). Cap it:
    ``close()`` already stopped accepting, and any lingering handler finishes on
    its own. This only affects the (test-only) hang path — normal closes return
    well under the timeout.
    """
    server_cls = asyncio.base_events.Server
    if getattr(server_cls.wait_closed, "_uterm_bounded", False):
        return
    _orig = server_cls.wait_closed

    async def _bounded(self: Any) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_orig(self), timeout=1.0)

    _bounded._uterm_bounded = True  # type: ignore[attr-defined]
    server_cls.wait_closed = _bounded  # type: ignore[method-assign]


_install_bounded_server_wait_closed()
_install_httpx_dev_principal_autoauth()
_install_websockets_dev_principal_autoauth()
