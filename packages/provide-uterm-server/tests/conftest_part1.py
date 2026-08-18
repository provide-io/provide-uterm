#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Shared pytest fixtures for provide-uterm tests."""

from __future__ import annotations

import asyncio
import importlib.resources
import importlib.util
import json

# Ensure this repo's src/provide package wins over sibling workspaces on sys.path.
# Skip in mutant context — mutmut's root conftest already prepends mutants/src/
# with trampolined modules; overriding it here would load non-trampolined copies
# and cause all connectors mutants to report no_tests.
import os as _os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

if not _os.environ.get("MUTANT_UNDER_TEST"):
    _PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
    _PROJECT_SRC_STR = str(_PROJECT_SRC)
    if _PROJECT_SRC_STR in sys.path:
        sys.path.remove(_PROJECT_SRC_STR)
    sys.path.insert(0, _PROJECT_SRC_STR)
    _loaded_provide = sys.modules.get("provide")
    if _loaded_provide is not None:
        loaded_path = str(getattr(_loaded_provide, "__file__", ""))
        if "/provide-uterm/src/provide/" not in loaded_path:
            for name in list(sys.modules):
                if name == "provide" or name.startswith("provide."):
                    del sys.modules[name]

from provide.uterm.server.bridge.hub import TermHub

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator, Iterator


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def load_example_server_module() -> Any:
    """Load scripts/example_server.py directly so tests do not depend on sys.path packaging."""
    module_name = "_codex_example_server_test_module"
    import sys

    if module_name in sys.modules:
        return sys.modules[module_name]

    path = Path(__file__).resolve().parents[3] / "scripts" / "example_server.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load example server module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="session")
def _redirect_dev_token_path(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep auto-issued dev tokens out of ~/.cache during tests.

    setup_dev_idp() writes a JWT to ``UTERM_DEV_TOKEN_PATH`` (default
    ``~/.cache/uterm/dev_token``). With the default mode now being
    ``dev_token``, every create_server_app() call would either pollute
    the user's home dir or race with parallel test workers. Redirecting
    via env var gives each test session its own location.
    """
    path = tmp_path_factory.mktemp("dev_token") / "dev_token"
    _os.environ["UTERM_DEV_TOKEN_PATH"] = str(path)


def _install_testclient_dev_token_autoauth() -> None:
    """Auto-attach the dev-token bearer to every TestClient instance.

    Tests build apps via ``create_server_app(default_server_config())``,
    which mints a JWT through ``setup_dev_idp`` and writes it to
    ``UTERM_DEV_TOKEN_PATH``. Without this patch every test would have to
    read the file and inject ``Authorization: Bearer ...`` itself.

    Tests that exercise the unauthenticated codepath (e.g. token-missing
    coverage in ``test_auth*``) clear the header explicitly:
    ``client.headers.pop("Authorization", None)``.
    """
    from starlette.testclient import TestClient as _TestClient

    if getattr(_TestClient, "_uterm_devtoken_patched", False):
        return

    _original_init = _TestClient.__init__

    def _patched_init(self: _TestClient, *args: Any, **kwargs: Any) -> None:
        _original_init(self, *args, **kwargs)
        token_path_str = _os.environ.get("UTERM_DEV_TOKEN_PATH")
        if not token_path_str:
            return
        token_path = Path(token_path_str)
        try:
            token = token_path.read_text().strip()
        except OSError:
            return
        if token and "Authorization" not in self.headers:
            self.headers["Authorization"] = f"Bearer {token}"

    _TestClient.__init__ = _patched_init  # type: ignore[method-assign]
    _TestClient._uterm_devtoken_patched = True  # type: ignore[attr-defined]


_install_testclient_dev_token_autoauth()


@pytest.fixture
def http_mock_router() -> Iterator[Any]:
    """An active http_mock router, for tests that take it as a fixture."""
    from tests.helpers import http_mock

    with http_mock.mock as router:
        yield router


def _install_httpx_dev_principal_autoauth() -> None:
    """Auto-attach admin header-mode credentials to test-only httpx clients.

    The live-server fixtures (``reference_server``, ``live_reference_server``)
    run in ``header`` auth mode for tests, where principal/role come from
    ``X-Uterm-Principal``/``X-Uterm-Role`` headers. Without this patch every
    test would have to attach them by hand.

    Per-request headers override the defaults, so tests that want to
    exercise unauthenticated or non-admin paths still can by passing their
    own ``X-Uterm-Principal``/``X-Uterm-Role`` explicitly.

    Patch httpx2's Client and AsyncClient -- the classes
    ``starlette.testclient.TestClient`` actually subclasses. This used to patch
    httpx instead, and when starlette moved its TestClient to httpx2 (a
    separate distribution with an unrelated class hierarchy) no TestClient
    received the admin headers, header-mode auth resolved ``anonymous``, and
    323 server tests failed with 401 while line coverage stayed green. The repo
    is now single-stack, so there is one module to patch and no way for the two
    to drift apart again.
    """
    _defaults = {"X-Uterm-Principal": "admin", "X-Uterm-Role": "admin"}

    def _patch(cls: type) -> None:
        if getattr(cls, "_uterm_devprincipal_patched", False):
            return
        _orig_init = cls.__init__

        def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            _orig_init(self, *args, **kwargs)
            for header, value in _defaults.items():
                if header not in self.headers:
                    self.headers[header] = value

        cls.__init__ = _patched_init  # type: ignore[method-assign]
        cls._uterm_devprincipal_patched = True  # type: ignore[attr-defined]

    import httpx2

    _patch(httpx2.Client)
    _patch(httpx2.AsyncClient)


_install_httpx_dev_principal_autoauth()


def _install_websockets_dev_principal_autoauth() -> None:
    """Auto-attach admin header-mode credentials to websockets test clients.

    Sibling to the httpx patch above. Tests connect to the live header-mode
    reference server via :func:`websockets.connect`, which doesn't share
    httpx's default-headers mechanism. Per-call ``additional_headers``
    overrides win, so unauthenticated/non-admin paths can still be tested
    by passing explicit headers.
    """
    import websockets as _websockets
    import websockets.asyncio.client as _ws_client

    if getattr(_ws_client.connect, "_uterm_devprincipal_patched", False):
        return

    _defaults = {"X-Uterm-Principal": "admin", "X-Uterm-Role": "admin"}
    _worker_bearer = "Bearer test-bearer-token-32-chars-long-x"
    _orig = _ws_client.connect

    def _patched_connect(*args: Any, **kwargs: Any) -> Any:
        # First positional arg or `uri=` kwarg is the websocket URL.
        uri = args[0] if args else kwargs.get("uri", "")
        provided = kwargs.get("additional_headers") or {}
        merged = dict(_defaults)
        # Worker WS uses bearer auth (worker_bearer_token), not X-Uterm-*.
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


_install_websockets_dev_principal_autoauth()


@pytest.fixture()
def free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# event_loop fixture intentionally omitted: pytest-asyncio >= 0.21 with
# asyncio_mode="auto" manages per-test loops automatically.  A custom
# event_loop fixture at function scope conflicts with the auto-mode
# machinery and can cause asyncio.Lock cross-loop corruption.


# ---------------------------------------------------------------------------
# Live TermHub — async, function-scoped (for WS integration tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def live_hub() -> AsyncGenerator[tuple[TermHub, str], None]:
    """Async function-scoped fixture: real TermHub on a random port via uvicorn.

    Yields ``(hub, base_url)`` — e.g. ``(hub, "http://127.0.0.1:54321")``.

    The server runs as an asyncio task inside the test's event loop so that
    fixtures and tests share the same loop (important for asyncio.Lock sanity).
    """
    hub = TermHub(resolve_browser_role=lambda _ws, _worker_id: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not server.started:
            if loop.time() > deadline:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=2.0)
                raise RuntimeError("live_hub: uvicorn did not start within 5 s")
            await asyncio.sleep(0.05)

        port: int = server.servers[0].sockets[0].getsockname()[1]
        yield hub, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Playwright session server — sync, session-scoped
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def hijack_server() -> Generator[tuple[str, TermHub], None, None]:
    """Session-scoped sync fixture: TermHub + static UI server for Playwright tests.

    Yields ``(base_url, hub)``.

    Also exposes ``GET /test-page/{worker_id}`` — a minimal HTML page that
    mounts the ProvideHijack widget with ``heartbeatInterval: 500`` so heartbeat
    tests complete quickly.
    """
    from starlette.staticfiles import StaticFiles

    hub = TermHub(resolve_browser_role=lambda _ws, _worker_id: "admin")
    app = FastAPI()
    app.include_router(hub.create_router())

    frontend_path = importlib.resources.files("provide.uterm.server") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    @app.get("/test-page/{worker_id}", response_class=HTMLResponse)
    async def test_page(worker_id: str) -> str:
        # heartbeatInterval=500 ms so heartbeat tests don't take >5 s.
        return (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            "<style>*{margin:0;padding:0;box-sizing:border-box}"
            "html,body{width:100%;height:100dvh;background:#0b0f14}"
            "#app{width:100%;height:100%}</style></head>"
            "<body><div id='app'></div>"
            "<script type='module'>"
            "import { ProvideHijack } from '/ui/hijack.js';"
            "new ProvideHijack(document.getElementById('app'),"
            f"{{workerId:{json.dumps(worker_id)},heartbeatInterval:500}});"
            "</script>"
            "</body></html>"
        )

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("hijack_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    port: int = server.servers[0].sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    yield base_url, hub

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def example_server() -> Generator[str, None, None]:
    """Function-scoped fixture: run a fresh interactive example server per test."""
    example_server_module = load_example_server_module()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    base_url = f"http://127.0.0.1:{port}"
    example_server_module._runtime_base_url = base_url
    example_server_module._reset_all_sessions()

    config = uvicorn.Config(example_server_module.app, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("example_server: uvicorn failed to start within 10 s")
        time.sleep(0.05)

    worker_deadline = time.monotonic() + 10.0
    while not example_server_module._get_or_create_session(example_server_module._DEFAULT_WORKER_ID).connected:
        if time.monotonic() > worker_deadline:
            raise RuntimeError("example_server: worker failed to connect within 10 s")
        time.sleep(0.05)

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)
