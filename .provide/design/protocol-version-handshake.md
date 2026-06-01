# Design: Bridge Protocol Version Handshake

> **Status (2026-06): IMPLEMENTED — Option B shipped.** Range negotiation
> is live on `main`. `contracts.py` now declares
> `MIN_/MAX_/PREFERRED_PROTOCOL_VERSION` (with `CURRENT_PROTOCOL_VERSION`
> kept as an alias of `PREFERRED`) plus `negotiate_protocol_version()`.
> `worker_hello` advertises the `{min, max, preferred}` range
> (`shell/terminal/_output.py`), and all four connectors delegate to it.
> The server negotiates and closes the WS with code **1002** +
> `{"type":"error","reason":"protocol_mismatch", …}` on no overlap
> (`bridge/routes/websockets_impl.py`); `set_worker_hello`
> (`bridge/hub/connection.py`) persists the negotiated version on the
> worker entry. The **Problem** and **Files that would change** sections
> below are the original (pre-implementation) proposal text and are
> retained for history — note the `websockets.py:NNN` line citations now
> resolve to the split `websockets_impl.py` (see notes inline).

## Problem

`CURRENT_PROTOCOL_VERSION = 1` was declared at `packages/provide-uterm/src/provide/uterm/bridge/contracts.py` and stamped onto the server's `hello` frame to the browser (the hello-frame builder now lives in `bridge/routes/browser_handlers.py:430`; the worker-receive logic that was historically cited as `websockets.py:156-163` / `hub/connections.py:179-189` is now in `bridge/routes/websockets_impl.py` and `bridge/hub/connection.py:196`). At the time of writing the value was **never validated** — the server logged the worker's reported version but accepted any value, including `None`. Workers built against the in-tree connectors (`server/connectors/{shell,ssh,telnet,websocket}.py:115/139/74/76`) and `provide/uterm/shell/terminal/_output.py:18-20` send a `worker_hello` that omits `protocol_version` entirely. Browsers don't return a `protocol_version` to the server. Any future bump (e.g. control-frame shape change) will silently mis-parse on older peers.

## Options

| Option | Pro | Con |
|---|---|---|
| **A. Single integer, strict match.** Each side echoes `protocol_version`; mismatch → close with code 1002 + structured error frame. | Trivial to implement. Crisp failure mode. | Lockstep upgrades — a single bumped client breaks the fleet until the server is upgraded. |
| **B. Range negotiation.** Each side advertises `{min_version, max_version, preferred}`; intersection chosen; close on empty intersection. | Allows rolling upgrades, mixed deployments. Matches WebSocket-subprotocol style negotiation. | More surface area, more tests, more state on `WorkerEntry`. Harder to reason about feature gating downstream. |
| **C. WebSocket subprotocol header.** Encode versions in `Sec-WebSocket-Protocol` (`uterm.v1`, `uterm.v2`); browser/worker connect-time selects. | Fails fast at handshake, before app code runs. Standards-aligned. | Browsers via `WebSocket()` can only pass a single list of strings — works but awkward to evolve. Pushes negotiation into proxy/load-balancer layer (CF Worker re-implements). |

## Recommendation

**B (range negotiation) at the app layer**, not the subprotocol layer. Rationale: this project routinely runs mixed worker/server versions during tunnel scenarios and CI matrix tests, and Option A's lockstep requirement will produce flaky upgrades. The CF Worker adapter (`packages/provide-uterm-cloudflare`) already has its own framing constraints, so keeping negotiation in JSON control frames (not subprotocol) gives one code path across both backends.

Concrete shape:

```json
{"type": "worker_hello", "input_mode": "open",
 "protocol": {"min": 1, "max": 1, "preferred": 1}, "ts": ...}
```

Server replies in `make_hello_frame(...)` with `"protocol": {"selected": 1, "server_min": 1, "server_max": 1}`. Mismatch → close 1002 with `{"type":"error","reason":"protocol_mismatch","client":...,"server":...}` sent before close.

## Files that would change

- `packages/provide-uterm/src/provide/uterm/bridge/contracts.py:24` — replace single `CURRENT_PROTOCOL_VERSION` with `MIN_PROTOCOL_VERSION`, `MAX_PROTOCOL_VERSION`, `PREFERRED_PROTOCOL_VERSION`.
- `packages/provide-uterm-server/src/provide/uterm/server/bridge/frames.py:100` — extend the WorkerHello typed dict / `make_hello_frame` to include the protocol range field.
- `packages/provide-uterm-server/src/provide/uterm/server/bridge/routes/websockets_impl.py` (negotiation at ~:108-133; `websockets.py` is now only a re-export shim) — parse client range, negotiate, close on mismatch.
- `packages/provide-uterm-server/src/provide/uterm/server/bridge/routes/browser_handlers.py:430` (`make_hello_frame`) — emit server range in browser hello.
- `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/connection.py:196` (`set_worker_hello`) — store negotiated version on the worker entry, expose via `WorkerEntry.protocol_version`.
- All worker-hello emitters:
  - `packages/provide-uterm/src/provide/uterm/shell/terminal/_output.py:18-20`
  - `packages/provide-uterm-server/src/provide/uterm/server/connectors/shell.py:115`
  - `packages/provide-uterm-server/src/provide/uterm/server/connectors/ssh.py:139`
  - `packages/provide-uterm-server/src/provide/uterm/server/connectors/telnet.py:74`
  - `packages/provide-uterm-server/src/provide/uterm/server/connectors/websocket.py:76`
- Frontend `packages/provide-uterm-frontend/` (search `protocol_version`) — surface mismatch reason in the disconnect banner.
- Cloudflare adapter `packages/provide-uterm-cloudflare/` — mirror the negotiation logic so both backends agree.
- New tests under `packages/provide-uterm-server/tests/bridge/` covering: range overlap, no-overlap close code, missing field (assume `{min:1,max:1}`).

## Open question for maintainer

Should the CF Worker pin its own range tighter than the FastAPI server, to allow CF-only feature gates? If so, the response frame needs a `backend` discriminator. Otherwise keep them in lockstep.
