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
    from collections.abc import AsyncGenerator, Generator


# ---------------------------------------------------------------------------
# Test-only auto-auth — mirrors packages/provide-uterm-server/tests/conftest.py
# ---------------------------------------------------------------------------


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
    """Attach admin header-mode credentials to test websockets clients.

    Worker WS uses bearer auth (worker_bearer_token); browser/control WS uses
    X-Uterm-Principal/Role. The patch routes appropriately based on URI path.
    Per-call ``additional_headers`` overrides win.
    """
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


_install_httpx_dev_principal_autoauth()
_install_websockets_dev_principal_autoauth()


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

    Rate limits are raised well above production defaults so stress tests
    (hijack acquire/release loops, fan-out traffic) measure correctness
    under load, not the rate-limit gate.
    """
    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
        browser_control_rate_limit_per_sec=1000,
    )
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
def hijack_server() -> Generator[tuple[str, object], None, None]:
    """Session-scoped server for Playwright hijack tests.

    Yields ``(base_url, hub_or_none)``.

    * Default (no ``UTERM_TEST_BACKEND`` / ``python``): in-process FastAPI
      TermHub with ``/test-page`` + ``/ui`` (historic, fast CI).
    * ``UTERM_TEST_BACKEND=go|csharp`` (or ``python`` with
      ``UTERM_MULTI_BACKEND=1``): real language server subprocess under
      ``UTERM_TEST_MODE=1``. Test HTML/UI assets are page.route'd (see
      ``playwright/ui_routes.py``).
    """
    import os

    multi = os.environ.get("UTERM_MULTI_BACKEND", "").strip() in ("1", "true", "yes")
    backend = os.environ.get("UTERM_TEST_BACKEND", "python").strip().lower() or "python"
    if multi or backend in ("go", "csharp"):
        os.environ["UTERM_TEST_BACKEND"] = backend if backend in ("python", "go", "csharp") else "python"
        from .playwright.backend_server import WORKER_BEARER, spawn_backend_server

        os.environ["UTERM_TEST_WORKER_BEARER"] = WORKER_BEARER
        with spawn_backend_server() as srv:
            yield srv.base_url, None
        return

    from starlette.staticfiles import StaticFiles

    hub = TermHub(
        resolve_browser_role=lambda _ws, _worker_id: "admin",
        browser_control_rate_limit_per_sec=1000,
    )
    app = FastAPI()
    app.include_router(hub.create_router())

    frontend_path = importlib.resources.files("provide.uterm.server") / "frontend"
    app.mount("/ui", StaticFiles(directory=str(frontend_path), html=True), name="ui")

    @app.get("/test-page/{worker_id}", response_class=HTMLResponse)
    async def test_page(worker_id: str) -> str:
        from provide.uterm.server.ui import _resolve_vanilla_asset

        script_path = _resolve_vanilla_asset("src/hijack.ts")
        return (
            "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
            "<script>"
            "window.__deepQuery=(sel)=>{const s=(r)=>{const d=r.querySelector(sel);if(d)return d;"
            "for(const e of r.querySelectorAll('*')){if(e.shadowRoot){const f=s(e.shadowRoot);if(f)return f;}}return null;};"
            "return s(document);};"
            "window.__deepQueryAll=(sel)=>{const o=[];const s=(r)=>{for(const e of r.querySelectorAll(sel))o.push(e);"
            "for(const e of r.querySelectorAll('*')){if(e.shadowRoot)s(e.shadowRoot);}};s(document);return o;};"
            "</script>"
            "<style>*{margin:0;padding:0;box-sizing:border-box}"
            "html,body{width:100%;height:100dvh;background:#0b0f14}"
            "#app,uterm-session{display:block;width:100%;height:100%}</style></head>"
            "<body><div id='app'></div>"
            "<script type='module'>"
            f"import '/ui/{script_path}';"
            "customElements.whenDefined('uterm-session').then(() => {"
            "  const el = document.createElement('uterm-session');"
            "  el.id = 'app-root';"
            f"  el.config = {{workerId:{json.dumps(worker_id)},heartbeatInterval:500}};"
            "  document.getElementById('app').appendChild(el);"
            "  el.connect();"
            "  window.demoHijack = el;"
            "});"
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
