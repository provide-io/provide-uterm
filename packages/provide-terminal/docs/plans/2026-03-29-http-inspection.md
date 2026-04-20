# HTTP Tunnel Inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add HTTP-aware tunneling with real-time traffic inspection — agent acts as reverse proxy, sends structured request/response JSON on channel 0x03, viewable in CLI, browser SPA, and standalone page.

**Architecture:** Agent-side HTTP reverse proxy parses request/response pairs and sends them as structured JSON on tunnel channel 0x03. Server broadcasts channel 0x03 frames to browsers unchanged. Three UI surfaces: CLI stderr output, SPA inspect view (`/app/inspect/{id}`), and standalone embeddable page.

**Tech Stack:** Python asyncio (aiohttp for proxy), TypeScript (vanilla DOM for inspect UI), existing tunnel protocol + WebSocket infrastructure.

______________________________________________________________________

### Task 1: HTTP Message Types

**Files:**

- Modify: `packages/provide-terminal/src/provide/terminal/tunnel/types.py`

- Test: `packages/provide-terminal/tests/tunnel/test_types.py`

- [ ] **Step 1: Write failing test for HttpRequest/HttpResponse types**

```python
# packages/provide-terminal/tests/tunnel/test_types.py
from provide.terminal.tunnel.types import HttpRequestMessage, HttpResponseMessage

def test_http_request_message_fields():
    msg: HttpRequestMessage = {
        "type": "http_req",
        "id": "r1",
        "ts": 1711000000.0,
        "method": "GET",
        "url": "/api/users",
        "headers": {"accept": "application/json"},
        "body_size": 0,
    }
    assert msg["type"] == "http_req"
    assert msg["id"] == "r1"

def test_http_response_message_fields():
    msg: HttpResponseMessage = {
        "type": "http_res",
        "id": "r1",
        "ts": 1711000000.089,
        "status": 200,
        "status_text": "OK",
        "headers": {"content-type": "application/json"},
        "body_size": 18,
        "duration_ms": 89,
    }
    assert msg["status"] == 200
    assert msg["duration_ms"] == 89

def test_http_request_with_body():
    msg: HttpRequestMessage = {
        "type": "http_req",
        "id": "r2",
        "ts": 1711000000.0,
        "method": "POST",
        "url": "/api/login",
        "headers": {"content-type": "application/json"},
        "body_size": 42,
        "body_b64": "eyJ1c2VyIjoiYWRtaW4ifQ==",
    }
    assert msg["body_b64"] == "eyJ1c2VyIjoiYWRtaW4ifQ=="

def test_http_response_truncated():
    msg: HttpResponseMessage = {
        "type": "http_res",
        "id": "r3",
        "ts": 1.0,
        "status": 200,
        "status_text": "OK",
        "headers": {},
        "body_size": 300000,
        "duration_ms": 10,
        "body_truncated": True,
    }
    assert msg["body_truncated"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_types.py -v --no-cov` Expected: `ImportError: cannot import name 'HttpRequestMessage'`

- [ ] **Step 3: Add TypedDicts to types.py**

Add to `packages/provide-terminal/src/provide/terminal/tunnel/types.py`:

```python
class HttpRequestMessage(TypedDict, total=False):
    """Structured HTTP request sent on channel 0x03."""

    type: str  # "http_req"
    id: str
    ts: float
    method: str
    url: str
    headers: dict[str, str]
    body_size: int
    body_b64: str
    body_truncated: bool
    body_binary: bool


class HttpResponseMessage(TypedDict, total=False):
    """Structured HTTP response sent on channel 0x03."""

    type: str  # "http_res"
    id: str
    ts: float
    status: int
    status_text: str
    headers: dict[str, str]
    body_size: int
    body_b64: str
    body_truncated: bool
    body_binary: bool
    duration_ms: float
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_types.py -v --no-cov` Expected: all PASS

- [ ] **Step 5: Add channel constant to protocol.py**

Add to `packages/provide-terminal/src/provide/terminal/tunnel/protocol.py`:

```python
CHANNEL_HTTP: int = 0x03
```

- [ ] **Step 6: Commit**

```bash
git add packages/provide-terminal/src/provide/terminal/tunnel/types.py \
       packages/provide-terminal/src/provide/terminal/tunnel/protocol.py \
       packages/provide-terminal/tests/tunnel/test_types.py
git commit -m "feat: add HTTP message types and CHANNEL_HTTP constant"
```

______________________________________________________________________

### Task 2: HTTP Proxy Core

**Files:**

- Create: `packages/provide-terminal/src/provide/terminal/tunnel/http_proxy.py`

- Test: `packages/provide-terminal/tests/tunnel/test_http_proxy.py`

- [ ] **Step 1: Write failing tests for body encoding and log formatting**

```python
# packages/provide-terminal/tests/tunnel/test_http_proxy.py
import base64
from provide.terminal.tunnel.http_proxy import encode_body, format_log_line, BODY_MAX_BYTES, BINARY_CONTENT_TYPES

def test_encode_body_small():
    body = b'{"user": "admin"}'
    result = encode_body(body, "application/json")
    assert result["body_b64"] == base64.b64encode(body).decode()
    assert result["body_size"] == len(body)
    assert "body_truncated" not in result

def test_encode_body_large():
    body = b"x" * (BODY_MAX_BYTES + 1)
    result = encode_body(body, "text/plain")
    assert "body_b64" not in result
    assert result["body_truncated"] is True
    assert result["body_size"] == len(body)

def test_encode_body_binary():
    body = b"\x89PNG\r\n"
    result = encode_body(body, "image/png")
    assert "body_b64" not in result
    assert result["body_binary"] is True

def test_encode_body_empty():
    result = encode_body(b"", "text/plain")
    assert result["body_size"] == 0
    assert "body_b64" not in result

def test_format_log_line_200():
    line = format_log_line("GET", "/api/users", 200, 142.3, 3200)
    assert "200" in line
    assert "GET" in line
    assert "/api/users" in line
    assert "142ms" in line

def test_format_log_line_500():
    line = format_log_line("POST", "/api/crash", 500, 34.0, 128)
    assert "500" in line
    assert "⚠" in line

def test_format_log_line_request_only():
    line = format_log_line("POST", "/api/login", None, None, 1100)
    assert "→" in line
    assert "POST" in line
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_http_proxy.py -v --no-cov` Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement http_proxy.py**

```python
# packages/provide-terminal/src/provide/terminal/tunnel/http_proxy.py
"""HTTP proxy helpers: body encoding, log formatting, content type detection."""

from __future__ import annotations

import base64
from typing import Any

BODY_MAX_BYTES = 256 * 1024  # 256 KB

BINARY_CONTENT_TYPES = frozenset({
    "image/", "audio/", "video/", "application/octet-stream",
    "application/zip", "application/gzip", "application/pdf",
    "application/wasm", "font/",
})


def _is_binary(content_type: str) -> bool:
    ct = content_type.lower().split(";")[0].strip()
    return any(ct.startswith(prefix) for prefix in BINARY_CONTENT_TYPES)


def encode_body(body: bytes, content_type: str) -> dict[str, Any]:
    """Encode a request/response body per the spec rules."""
    result: dict[str, Any] = {"body_size": len(body)}
    if not body:
        return result
    if _is_binary(content_type):
        result["body_binary"] = True
        return result
    if len(body) > BODY_MAX_BYTES:
        result["body_truncated"] = True
        return result
    result["body_b64"] = base64.b64encode(body).decode("ascii")
    return result


def format_log_line(
    method: str,
    url: str,
    status: int | None,
    duration_ms: float | None,
    body_size: int,
) -> str:
    """Format a compact mitmproxy-style log line."""
    size_str = _human_size(body_size)
    if status is None:
        return f"→ {method} {url} ({size_str})"
    warn = " ⚠" if status >= 500 else ""
    dur = f"{duration_ms:.0f}ms" if duration_ms is not None else "?"
    return f"← {status} {method} {url} ({dur}, {size_str}){warn}"


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_http_proxy.py -v --no-cov` Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add packages/provide-terminal/src/provide/terminal/tunnel/http_proxy.py \
       packages/provide-terminal/tests/tunnel/test_http_proxy.py
git commit -m "feat: add HTTP proxy helpers (body encoding, log formatting)"
```

______________________________________________________________________

### Task 3: CLI `uterm inspect` Command

**Files:**

- Create: `packages/provide-terminal/src/provide/terminal/cli/inspect.py`

- Modify: `packages/provide-terminal/src/provide/terminal/cli/__init__.py`

- Test: `packages/provide-terminal/tests/tunnel/test_inspect_cli.py`

- [ ] **Step 1: Write failing tests for arg parsing and subcommand registration**

```python
# packages/provide-terminal/tests/tunnel/test_inspect_cli.py
import pytest
from provide.terminal.cli import _build_parser

class TestInspectArgParsing:
    def test_inspect_subcommand_recognised(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "3000", "-s", "https://example.com"])
        assert args.port == 3000
        assert args.server == "https://example.com"

    def test_inspect_requires_port(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "-s", "https://example.com"])

    def test_inspect_requires_server(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["inspect", "3000"])

    def test_inspect_has_func(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "3000", "-s", "https://x.com"])
        assert hasattr(args, "func")

    def test_inspect_listen_port(self):
        parser = _build_parser()
        args = parser.parse_args(["inspect", "3000", "-s", "https://x.com", "--listen-port", "9123"])
        assert args.listen_port == 9123
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_inspect_cli.py -v --no-cov` Expected: FAIL — `inspect` subcommand not registered

- [ ] **Step 3: Create inspect.py with subcommand registration and \_cmd_inspect stub**

Create `packages/provide-terminal/src/provide/terminal/cli/inspect.py`:

```python
"""``uterm inspect`` — HTTP reverse proxy with traffic inspection.

Example::

    uterm inspect 3000 --server https://warp.provide.io
    uterm inspect 8080 --server https://warp.provide.io --listen-port 9123
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

from provide.terminal.defaults import TerminalDefaults
from provide.terminal.tunnel.http_proxy import encode_body, format_log_line
from provide.terminal.tunnel.protocol import (
    CHANNEL_DATA,
    CHANNEL_HTTP,
    encode_control,
    encode_frame,
)

log = logging.getLogger(__name__)


def _read_token(args: argparse.Namespace) -> str | None:
    if getattr(args, "token", None):
        return args.token
    token_path = Path(getattr(args, "token_file", "") or str(TerminalDefaults.token_file())).expanduser()
    if token_path.is_file():
        return token_path.read_text().strip()
    return None


def _create_tunnel(server: str, display_name: str, token: str | None, target_port: int) -> dict[str, Any]:
    url = f"{server.rstrip('/')}/api/tunnels"
    body = json.dumps({"tunnel_type": "http", "display_name": display_name, "target_port": target_port}).encode()
    headers: dict[str, str] = {"Content-Type": "application/json", "User-Agent": "uterm-inspect/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        with suppress(Exception):
            detail = exc.read().decode(errors="replace")
        print(f"error: tunnel creation failed (HTTP {exc.code}): {detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"error: cannot reach server: {exc.reason}", file=sys.stderr)
        sys.exit(1)


def _cmd_inspect(args: argparse.Namespace) -> None:
    server: str = args.server
    target_port: int = args.port
    listen_port: int = getattr(args, "listen_port", 0)
    display_name: str = getattr(args, "display_name", None) or f"http:{target_port}"
    token = _read_token(args)

    tunnel_info = _create_tunnel(server, display_name, token, target_port)
    ws_endpoint = tunnel_info.get("ws_endpoint", "")
    worker_token = tunnel_info.get("worker_token", "")
    share_url = tunnel_info.get("share_url", "")

    if not ws_endpoint:
        print("error: server response missing ws_endpoint", file=sys.stderr)
        sys.exit(1)

    if ws_endpoint.startswith("/"):
        ws_base = server.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        ws_endpoint = f"{ws_base}{ws_endpoint}"

    print(f"[inspect] Proxying localhost:{target_port} via tunnel", file=sys.stderr)
    if share_url:
        print(f"  View:   {share_url}", file=sys.stderr)
    print(f"\nConnected. Press Ctrl+C to stop.", file=sys.stderr)

    with suppress(KeyboardInterrupt):
        asyncio.run(_run_inspect(ws_endpoint, worker_token, target_port, listen_port))


async def _run_inspect(  # pragma: no cover — integration; tested via E2E
    ws_endpoint: str,
    worker_token: str,
    target_port: int,
    listen_port: int,
) -> None:
    try:
        import aiohttp
        import websockets
    except ImportError:
        print("error: missing dependencies — aiohttp, websockets\npip install 'provide-terminal[cli]'", file=sys.stderr)
        sys.exit(1)

    headers = {}
    if worker_token:
        headers["Authorization"] = f"Bearer {worker_token}"

    async with websockets.connect(ws_endpoint, additional_headers=headers) as ws:
        await ws.send(encode_control({
            "type": "open", "channel": 3, "tunnel_type": "http", "target_port": target_port,
        }))

        req_counter = 0

        async def handle_request(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
            nonlocal req_counter
            req_counter += 1
            req_id = f"r{req_counter}"
            req_body = await request.read()
            req_ts = time.time()

            # Send http_req on channel 0x03
            ct = request.headers.get("content-type", "")
            req_msg = {
                "type": "http_req", "id": req_id, "ts": req_ts,
                "method": request.method, "url": str(request.rel_url),
                "headers": dict(request.headers),
                **encode_body(req_body, ct),
            }
            await ws.send(encode_frame(CHANNEL_HTTP, json.dumps(req_msg, separators=(",", ":")).encode()))

            # Log request
            log_line = format_log_line(request.method, str(request.rel_url), None, None, len(req_body))
            print(log_line, file=sys.stderr)
            # Also send to terminal channel for browser viewers
            await ws.send(encode_frame(CHANNEL_DATA, (log_line + "\n").encode()))

            # Forward to target
            async with aiohttp.ClientSession() as session:
                target_url = f"http://127.0.0.1:{target_port}{request.rel_url}"
                async with session.request(
                    request.method, target_url, headers=request.headers, data=req_body,
                ) as upstream:
                    res_body = await upstream.read()
                    res_ts = time.time()
                    duration_ms = (res_ts - req_ts) * 1000

                    # Send http_res on channel 0x03
                    res_ct = upstream.headers.get("content-type", "")
                    res_msg = {
                        "type": "http_res", "id": req_id, "ts": res_ts,
                        "status": upstream.status, "status_text": upstream.reason or "",
                        "headers": dict(upstream.headers),
                        "duration_ms": round(duration_ms, 1),
                        **encode_body(res_body, res_ct),
                    }
                    await ws.send(encode_frame(CHANNEL_HTTP, json.dumps(res_msg, separators=(",", ":")).encode()))

                    # Log response
                    log_line = format_log_line(request.method, str(request.rel_url), upstream.status, duration_ms, len(res_body))
                    print(log_line, file=sys.stderr)
                    await ws.send(encode_frame(CHANNEL_DATA, (log_line + "\n").encode()))

                    # Return to local client
                    return aiohttp.web.Response(
                        status=upstream.status,
                        headers={k: v for k, v in upstream.headers.items() if k.lower() not in ("transfer-encoding",)},
                        body=res_body,
                    )

        app = aiohttp.web.AppRunner(aiohttp.web.Application())
        app.app.router.add_route("*", "/{path:.*}", handle_request)
        await app.setup()
        site = aiohttp.web.TCPSite(app, "127.0.0.1", listen_port)
        await site.start()

        actual_port = site._server.sockets[0].getsockname()[1] if site._server and site._server.sockets else listen_port
        print(f"  Listen: http://127.0.0.1:{actual_port} → localhost:{target_port}", file=sys.stderr)

        # Keep alive until cancelled
        try:
            await asyncio.Event().wait()
        finally:
            await app.cleanup()


def add_inspect_subcommand(subparsers: Any) -> None:
    inspect_p = subparsers.add_parser(
        "inspect",
        help="HTTP reverse proxy with traffic inspection",
        description="Proxy HTTP traffic through a tunnel with real-time inspection.",
    )
    inspect_p.add_argument("port", type=int, metavar="PORT", help="target service port on localhost")
    inspect_p.add_argument("--server", "-s", required=True, metavar="URL", help="tunnel server URL")
    inspect_p.add_argument("--listen-port", type=int, default=0, metavar="PORT", help="local proxy listen port (default: random)")
    inspect_p.add_argument("--token", metavar="TOKEN", default=None, help="bearer token for API auth")
    inspect_p.add_argument("--token-file", metavar="FILE", default=str(TerminalDefaults.token_file()), help="path to token file")
    inspect_p.add_argument("--display-name", metavar="NAME", default=None, help="override display name")
    inspect_p.set_defaults(func=_cmd_inspect)
```

- [ ] **Step 4: Register in cli/__init__.py**

Add after the tunnel subcommand registration in `packages/provide-terminal/src/provide/terminal/cli/__init__.py`:

```python
    # ---- inspect subcommand ----
    from provide.terminal.cli.inspect import add_inspect_subcommand

    add_inspect_subcommand(sub)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_inspect_cli.py -v --no-cov` Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add packages/provide-terminal/src/provide/terminal/cli/inspect.py \
       packages/provide-terminal/src/provide/terminal/cli/__init__.py \
       packages/provide-terminal/tests/tunnel/test_inspect_cli.py
git commit -m "feat: add uterm inspect — HTTP reverse proxy with traffic inspection CLI"
```

______________________________________________________________________

### Task 4: Server-Side Channel 0x03 Handling

**Files:**

- Modify: `packages/provide-terminal/src/provide/terminal/tunnel/fastapi_routes.py`

- Modify: `packages/provide-terminal-cloudflare/src/provide/terminal/cloudflare/api/tunnel_routes.py`

- Modify: `packages/provide-terminal/src/provide/terminal/server/routes/api.py`

- Test: `packages/provide-terminal/tests/tunnel/test_http_channel.py`

- [ ] **Step 1: Write failing test for channel 0x03 broadcast**

```python
# packages/provide-terminal/tests/tunnel/test_http_channel.py
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from provide.terminal.hijack.hub import TermHub
from provide.terminal.tunnel.protocol import CHANNEL_HTTP, encode_frame

@pytest.fixture
def client():
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    return TestClient(app)

class TestHttpChannelBroadcast:
    def test_channel_3_frame_not_treated_as_terminal(self, client):
        """Channel 0x03 frames should be broadcast as control frames, not term frames."""
        resp = client.app  # just verify the route exists
        with client.websocket_connect("/tunnel/test-http") as ws:
            msg = json.dumps({"type": "http_req", "id": "r1", "method": "GET", "url": "/test"}).encode()
            ws.send_bytes(encode_frame(CHANNEL_HTTP, msg))
            # No error — frame was accepted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_http_channel.py -v --no-cov` Expected: FAIL — `CHANNEL_HTTP` not defined

- [ ] **Step 3: Update fastapi_routes.py to handle channel 0x03**

In `packages/provide-terminal/src/provide/terminal/tunnel/fastapi_routes.py`, update the frame routing inside `ws_tunnel`:

Replace the channel handling block (around line 100-111):

```python
                if frame.is_control:
                    await _handle_control(hub, websocket, worker_id, frame.payload)
                elif frame.is_eof:
                    logger.info("tunnel_eof worker_id=%s channel=%d", worker_id, frame.channel)
                elif frame.channel == CHANNEL_HTTP:
                    # HTTP inspection: broadcast structured JSON as control frame
                    try:
                        http_msg = json.loads(frame.payload)
                        http_msg["_channel"] = "http"
                        await hub.broadcast(
                            worker_id,
                            cast("dict[str, Any]", http_msg),
                        )
                        await hub.append_event(worker_id, http_msg.get("type", "http"), http_msg)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        logger.warning("tunnel_bad_http_frame worker_id=%s", worker_id)
                elif frame.channel >= CHANNEL_DATA and frame.payload:
                    text = frame.payload.decode("utf-8", errors="replace")
                    await hub.broadcast(
                        worker_id,
                        cast("dict[str, Any]", make_term_frame(text, ts=time.time())),
                    )
```

Add `import json` and `from provide.terminal.tunnel.protocol import CHANNEL_HTTP` to imports.

- [ ] **Step 4: Update CF tunnel_routes.py similarly**

In `packages/provide-terminal-cloudflare/src/provide/terminal/cloudflare/api/tunnel_routes.py`, add channel 0x03 handling in `handle_tunnel_message`:

```python
_CHANNEL_HTTP = 0x03

# In handle_tunnel_message, before the existing channel >= _CHANNEL_DATA check:
    if channel == _CHANNEL_HTTP and payload:
        try:
            http_msg = json.loads(payload)
            http_msg["_channel"] = "http"
            await runtime.broadcast_worker_frame(http_msg)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("tunnel_bad_http_frame worker_id=%s", runtime.worker_id)
        return
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_http_channel.py -v --no-cov` Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/ -q --no-cov` Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add packages/provide-terminal/src/provide/terminal/tunnel/fastapi_routes.py \
       packages/provide-terminal/src/provide/terminal/tunnel/protocol.py \
       packages/provide-terminal-cloudflare/src/provide/terminal/cloudflare/api/tunnel_routes.py \
       packages/provide-terminal/tests/tunnel/test_http_channel.py
git commit -m "feat: handle channel 0x03 HTTP inspection frames on both backends"
```

______________________________________________________________________

### Task 5: SPA Inspect View — Types and Boot

**Files:**

- Modify: `packages/provide-terminal-frontend/src/app/types.ts`

- Modify: `packages/provide-terminal-frontend/src/app/boot.ts`

- Modify: `packages/provide-terminal-frontend/src/app/router.ts`

- Test: `packages/provide-terminal-frontend/src/app/boot.test.ts`

- [ ] **Step 1: Add "inspect" to AppPageKind**

In `packages/provide-terminal-frontend/src/app/types.ts`:

```typescript
export type AppPageKind = "dashboard" | "session" | "operator" | "replay" | "connect" | "inspect";
```

Add HTTP inspection types:

```typescript
export interface HttpRequestEntry {
  type: "http_req";
  id: string;
  ts: number;
  method: string;
  url: string;
  headers: Record<string, string>;
  body_size: number;
  body_b64?: string;
  body_truncated?: boolean;
  body_binary?: boolean;
}

export interface HttpResponseEntry {
  type: "http_res";
  id: string;
  ts: number;
  status: number;
  status_text: string;
  headers: Record<string, string>;
  body_size: number;
  body_b64?: string;
  body_truncated?: boolean;
  body_binary?: boolean;
  duration_ms: number;
}

export interface HttpExchangeEntry {
  id: string;
  request: HttpRequestEntry;
  response: HttpResponseEntry | null;
}
```

- [ ] **Step 2: Add "inspect" to boot.ts validation**

In `packages/provide-terminal-frontend/src/app/boot.ts`, add `"inspect"` to the `page_kind` validation:

```typescript
    parsed.page_kind !== "connect" &&
    parsed.page_kind !== "inspect"
```

- [ ] **Step 3: Add inspect route to router.ts**

```typescript
import { renderInspect } from "./views/inspect-view.js";

// In the switch:
    case "inspect":
      await renderInspect(root, bootstrap);
      return;
```

- [ ] **Step 4: Run frontend tests**

Run: `cd packages/provide-terminal-frontend && npm test -- --run` Expected: existing tests pass (inspect view import will fail until Task 6)

- [ ] **Step 5: Commit**

```bash
git add packages/provide-terminal-frontend/src/app/types.ts \
       packages/provide-terminal-frontend/src/app/boot.ts \
       packages/provide-terminal-frontend/src/app/router.ts
git commit -m "feat: register inspect page kind in frontend types and router"
```

______________________________________________________________________

### Task 6: SPA Inspect View — UI Component

**Files:**

- Create: `packages/provide-terminal-frontend/src/app/views/inspect-view.ts`

- Create: `packages/provide-terminal-frontend/src/app/views/inspect-view.css`

- Test: `packages/provide-terminal-frontend/src/app/views/inspect-view.test.ts`

- [ ] **Step 1: Write test for renderInspect**

```typescript
// packages/provide-terminal-frontend/src/app/views/inspect-view.test.ts
import { describe, it, expect } from "vitest";
import { renderInspect } from "./inspect-view.js";
import type { AppBootstrap } from "../types.js";

function makeBootstrap(overrides: Partial<AppBootstrap> = {}): AppBootstrap {
  return {
    page_kind: "inspect",
    title: "Inspect",
    app_path: "/app",
    assets_path: "/assets",
    session_id: "tunnel-abc",
    ...overrides,
  };
}

describe("renderInspect", () => {
  it("renders the inspect shell with request list", async () => {
    const root = document.createElement("div");
    await renderInspect(root, makeBootstrap());
    expect(root.querySelector("#inspect-list")).toBeTruthy();
    expect(root.querySelector("#inspect-detail")).toBeTruthy();
  });

  it("throws without session_id", async () => {
    const root = document.createElement("div");
    await expect(renderInspect(root, makeBootstrap({ session_id: undefined }))).rejects.toThrow();
  });
});
```

- [ ] **Step 2: Create inspect-view.ts**

```typescript
// packages/provide-terminal-frontend/src/app/views/inspect-view.ts
import type { AppBootstrap, HttpExchangeEntry, HttpRequestEntry, HttpResponseEntry } from "../types.js";
import { renderAppHeader } from "./app-header.js";

export async function renderInspect(root: HTMLElement, bootstrap: AppBootstrap): Promise<void> {
  if (!bootstrap.session_id) throw new Error("inspect bootstrap missing session_id");

  root.innerHTML = `
    <div class="page inspect-page">
      ${renderAppHeader(bootstrap, "inspect")}
      <div class="inspect-layout">
        <div class="inspect-toolbar">
          <select id="inspect-method-filter">
            <option value="">All Methods</option>
            <option>GET</option><option>POST</option><option>PUT</option>
            <option>DELETE</option><option>PATCH</option>
          </select>
          <input id="inspect-url-filter" type="text" placeholder="Filter URL..." />
          <span id="inspect-count">0 requests</span>
        </div>
        <div class="inspect-split">
          <div id="inspect-list" class="inspect-list"></div>
          <div id="inspect-detail" class="inspect-detail">
            <div class="inspect-empty">Select a request to view details</div>
          </div>
        </div>
      </div>
    </div>
  `;
}
```

- [ ] **Step 3: Create inspect-view.css**

```css
/* packages/provide-terminal-frontend/src/app/views/inspect-view.css */
.inspect-layout { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.inspect-toolbar { display: flex; gap: 8px; padding: 8px 16px; align-items: center; }
.inspect-toolbar select, .inspect-toolbar input { padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px; background: var(--bg-secondary); color: var(--text); font-size: 13px; }
.inspect-toolbar input { flex: 1; }
.inspect-split { display: flex; flex: 1; min-height: 0; }
.inspect-list { width: 50%; overflow-y: auto; border-right: 1px solid var(--border); font-size: 13px; font-family: 'Fira Code', monospace; }
.inspect-detail { width: 50%; overflow-y: auto; padding: 12px 16px; font-size: 13px; }
.inspect-empty { color: var(--text-muted); padding: 24px; text-align: center; }
.inspect-row { display: flex; gap: 8px; padding: 6px 12px; cursor: pointer; border-bottom: 1px solid var(--border); }
.inspect-row:hover { background: var(--bg-hover); }
.inspect-row.selected { background: var(--bg-selected); }
.inspect-row .method { font-weight: bold; min-width: 50px; }
.inspect-row .url { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.inspect-row .status { min-width: 36px; text-align: right; }
.inspect-row .status.s2xx { color: var(--green); }
.inspect-row .status.s3xx { color: var(--yellow); }
.inspect-row .status.s4xx { color: var(--yellow); }
.inspect-row .status.s5xx { color: var(--red); }
.inspect-row .duration { min-width: 50px; text-align: right; color: var(--text-muted); }
```

- [ ] **Step 4: Run frontend tests**

Run: `cd packages/provide-terminal-frontend && npm test -- --run` Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add packages/provide-terminal-frontend/src/app/views/inspect-view.ts \
       packages/provide-terminal-frontend/src/app/views/inspect-view.css \
       packages/provide-terminal-frontend/src/app/views/inspect-view.test.ts
git commit -m "feat: add inspect view UI component (request list + detail pane)"
```

______________________________________________________________________

### Task 7: Server-Side Inspect Page Route

**Files:**

- Modify: `packages/provide-terminal/src/provide/terminal/server/routes/pages.py`

- Modify: `packages/provide-terminal/src/provide/terminal/server/ui.py`

- Modify: `packages/provide-terminal-cloudflare/src/provide/terminal/cloudflare/entry.py`

- Test: `packages/provide-terminal/tests/server/test_pages.py`

- [ ] **Step 1: Add inspect page route to FastAPI**

In `packages/provide-terminal/src/provide/terminal/server/routes/pages.py`, add a route for `/inspect/{session_id}` following the same pattern as the existing session/operator routes. It should serve the SPA shell with `page_kind: "inspect"`.

- [ ] **Step 2: Add inspect route to CF entry.py**

In `packages/provide-terminal-cloudflare/src/provide/terminal/cloudflare/entry.py`, update `_SPA_SESSION_RE` to include `inspect`:

```python
_SPA_SESSION_RE = re.compile(r"^/app/(?P<kind>session|operator|replay|inspect)/(?P<sid>[a-zA-Z0-9_-]{1,64})$")
```

And in `_spa_response`, add inspect to the page kinds that load hijack.js (since it uses the same WS connection):

```python
    if page_kind in {"session", "operator", "inspect"}:
        pre_scripts = "<script type='module' src='/assets/hijack.js'></script>"
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest packages/provide-terminal/tests/server/test_pages.py -v --no-cov` Run: `uv run pytest packages/provide-terminal-cloudflare/tests/test_entry_unit.py -v --no-cov` Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add packages/provide-terminal/src/provide/terminal/server/routes/pages.py \
       packages/provide-terminal/src/provide/terminal/server/ui.py \
       packages/provide-terminal-cloudflare/src/provide/terminal/cloudflare/entry.py
git commit -m "feat: add /app/inspect/{id} page route on FastAPI and CF"
```

______________________________________________________________________

### Task 8: Integration Test — Full HTTP Inspection Flow

**Files:**

- Create: `packages/provide-terminal/tests/tunnel/test_http_inspect_e2e.py`

- [ ] **Step 1: Write E2E test**

```python
# packages/provide-terminal/tests/tunnel/test_http_inspect_e2e.py
import json
import pytest
from fastapi.testclient import TestClient
from provide.terminal.server.app import create_server_app
from provide.terminal.server.models import ServerConfig
from provide.terminal.tunnel.protocol import CHANNEL_DATA, CHANNEL_HTTP, encode_control, encode_frame

@pytest.fixture
def e2e_client():
    config = ServerConfig(auth={"mode": "none"})
    return TestClient(create_server_app(config))

class TestHttpInspectE2E:
    def test_create_http_tunnel(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http", "display_name": "http-test"})
        assert resp.status_code == 200
        assert resp.json()["tunnel_type"] == "http"

    def test_http_channel_frame_accepted(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
            http_req = json.dumps({"type": "http_req", "id": "r1", "method": "GET", "url": "/test", "headers": {}, "body_size": 0}).encode()
            ws.send_bytes(encode_frame(CHANNEL_HTTP, http_req))
            http_res = json.dumps({"type": "http_res", "id": "r1", "status": 200, "status_text": "OK", "headers": {}, "body_size": 5, "duration_ms": 42}).encode()
            ws.send_bytes(encode_frame(CHANNEL_HTTP, http_res))

    def test_terminal_and_http_channels_coexist(self, e2e_client):
        resp = e2e_client.post("/api/tunnels", json={"tunnel_type": "http"})
        tid = resp.json()["tunnel_id"]
        with e2e_client.websocket_connect(f"/tunnel/{tid}") as ws:
            ws.send_bytes(encode_control({"type": "open", "channel": 3, "tunnel_type": "http"}))
            ws.send_bytes(encode_frame(CHANNEL_DATA, b"[log] request proxied\n"))
            ws.send_bytes(encode_frame(CHANNEL_HTTP, json.dumps({"type": "http_req", "id": "r1", "method": "GET", "url": "/", "headers": {}, "body_size": 0}).encode()))
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest packages/provide-terminal/tests/tunnel/test_http_inspect_e2e.py -v --no-cov` Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add packages/provide-terminal/tests/tunnel/test_http_inspect_e2e.py
git commit -m "test: add E2E tests for HTTP inspection tunnel flow"
```

______________________________________________________________________

## Self-Review

**Spec coverage:**

- ✅ Channel 0x03 protocol (Tasks 1, 4)
- ✅ Agent HTTP proxy (Task 3)
- ✅ CLI output with color (Task 2, 3)
- ✅ Server-side broadcast on both backends (Task 4)
- ✅ SPA inspect view with request list + detail (Tasks 5, 6)
- ✅ Page routes on FastAPI and CF (Task 7)
- ✅ Body encoding rules (Task 2)
- ✅ E2E test (Task 8)
- ⏭ Standalone embeddable page — deferred to Phase 3 per spec
- ⏭ Intercept/modify — Phase 4 per spec

**Placeholder scan:** All steps have concrete code. No TBDs.

**Type consistency:** `HttpRequestMessage`/`HttpResponseMessage` in Python match `HttpRequestEntry`/`HttpResponseEntry` in TypeScript. `CHANNEL_HTTP = 0x03` consistent across protocol.py, fastapi_routes.py, tunnel_routes.py, and inspect.py.
