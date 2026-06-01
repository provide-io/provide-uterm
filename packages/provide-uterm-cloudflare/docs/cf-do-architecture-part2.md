```json
{"type":"http_req","id":"r1","ts":1711000000.0,
 "method":"POST","url":"/api/login",
 "headers":{"content-type":"application/json"},
 "body_size":42,"body_b64":"eyJ1c2VyIjoiYWRtaW4ifQ=="}

{"type":"http_res","id":"r1","ts":1711000000.089,
 "status":200,"status_text":"OK",
 "headers":{"content-type":"application/json"},
 "body_size":18,"body_b64":"eyJ0b2tlbiI6ImFiYyJ9",
 "duration_ms":89}
```

**Body rules:** < 256KB → base64 inline; > 256KB → `body_truncated: true`; binary content
types (image/*, audio/*, etc.) → `body_binary: true`. The actual proxy transparently
forwards the full body regardless of size.

### Token Security

Three tokens generated per tunnel session (`POST /api/tunnels`):

| Token | Purpose | Transport | Grants |
|-------|---------|-----------|--------|
| `worker_token` | Agent WSS auth | Bearer header | Tunnel connection |
| `share_token` | View URL | Query param or cookie | viewer role |
| `control_token` | Operator URL | Query param or cookie | operator role |

**Hardening:**
- **TTL**: Default 1 hour, configurable via `TunnelConfig.token_ttl_s`
- **Revocation**: `DELETE /api/tunnels/{id}/tokens`
- **Rotation**: `POST /api/tunnels/{id}/tokens/rotate` → new tokens, old invalidated
- **Timing-safe**: All comparisons use `secrets.compare_digest()`
- **Enumeration**: Share routes return 404 for both "not found" and "invalid token"
- **Cookie transport**: `token_transport: "both"` sets HttpOnly cookie + query param
- **IP binding**: Optional (`TunnelConfig.ip_binding`), validates source IP on access
- **Audit logging**: Structured logs on create/validate/expire/revoke/rotate

### CLI Commands

```
uterm share [cmd]              # Share terminal (channel 0x01)
uterm tunnel <port>            # Forward TCP port (channel 0x02)
uterm inspect <port>           # HTTP proxy + inspection (channels 0x01 + 0x03)
uterm watch <id-or-url>        # TUI viewer for existing tunnel (read-only, Textual)
```

### Browser Views

| Route | View | Data Source |
|-------|------|-------------|
| `/app/session/{id}` | Terminal viewer | Channel 0x01 via hijack widget |
| `/app/operator/{id}` | Terminal + controls | Channel 0x01 via hijack widget |
| `/app/inspect/{id}` | HTTP request list + detail | Channel 0x03 via direct WS |
| `/s/{id}?token=...` | Share viewer (CF) | Channel 0x01, token-authenticated |

### Key Files

| File | Role |
|------|------|
| `tunnel/protocol.py` | Binary frame encode/decode, channel constants |
| `tunnel/types.py` | `TunnelTokenState`, `HttpRequestMessage`, `HttpResponseMessage` |
| `tunnel/client.py` | Async WebSocket tunnel client with reconnect |
| `tunnel/pty_capture.py` | PTY spawn and TTY attach |
| `tunnel/http_proxy.py` | Body encoding rules, log formatting |
| `tunnel/fastapi_routes.py` | FastAPI `/tunnel/{id}` WS route (channels 0x01-0x03) |
| `cli/share.py` | `uterm share` CLI |
| `cli/tunnel.py` | `uterm tunnel` CLI |
| `cli/inspect.py` | `uterm inspect` CLI |
| CF `api/tunnel_routes.py` | DO binary frame handler |
| CF `api/_tunnel_api.py` | `POST /api/tunnels`, share URL auth |
| CF `entry/` | Route registration package: `/tunnel/`, `/s/`, `/api/tunnels`, `/app/inspect/` (dispatch in `entry/handlers.py` + `entry/registry.py`) |
| Frontend `provide-uterm-app` inspect UI (`components/inspect/*` + `useInspectWs.ts` + `stores/inspectStore.ts`) | Live HTTP request list + detail pane |

### Pyodide Runtime Fixes

| Issue | Fix |
|-------|-----|
| JS ArrayBuffer not `isinstance(bytes)` | `to_py()`/`to_bytes()` before check |
| `provide.uterm.cloudflare.api.*` import fails | `try/except` with flat path fallback |
| Worker WS triggers JWT decode | Skip `browser_role_for_request()` for `socket_role == "worker"` |

---

## Known Platform Quirks

| Quirk | Symptom | Fix |
|-------|---------|-----|
| `ctx.id.name()` returns `"default"` | Wrong KV key, wrong worker_id in events | `_lazy_init_worker_id()` extracts from URL |
| `kv.put(key, val, expirationTtl=N)` silently fails | KV entries never expire | Don't set TTL; delete explicitly on disconnect |
| `webSocketOpen()` async ops dropped on hibernation | KV not updated, hello not sent | Move critical writes to `fetch()` before 101 |
| `ws is self.worker_ws` always False after hibernation | Worker close not detected | Use `_socket_role(ws)` from attachment |
| `in-memory browser_sockets` empty after hibernation | Broadcasts go nowhere | Use `ctx.getWebSockets()` in `broadcast_to_browsers()` |
| CF Bot Fight Mode blocks `urllib.request` default UA | E2E HTTP helpers get 403 | Add `User-Agent` header in E2E helpers |
| Pyodide `importlib.resources` broken for bundled text | `FileNotFoundError` on asset load | Use `Path(__file__)` fallback in `assets.py` |
