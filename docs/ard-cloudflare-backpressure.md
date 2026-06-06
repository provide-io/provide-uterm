# ARD: End-to-end backpressure for the Cloudflare DO terminal relay

> Status: **accepted, implementation in progress** (Tier A first). Supersedes the
> ineffective `_queue_bytes` guard in `do/session_runtime/io.py::broadcast_to_browsers`.

## Problem

`broadcast_worker_frame → broadcast_to_browsers` (`do/session_runtime/io.py`) fans
worker output to browsers via `send_ws → _send_text → ws.send()`. In workerd
`ws.send()` is **synchronous and fire-and-forget** (`_send_text` even guards
`if inspect.isawaitable(result): await result`, and in CF the result is *not*
awaitable). So the existing guard:

```python
self._queue_bytes += msg_len
try:    await self.send_ws(ws, payload)   # resolves on enqueue, not drain
finally: self._queue_bytes = max(0, self._queue_bytes - msg_len)  # ...immediately
```

never accumulates across frames — it only ever reflects one in-flight frame, so it
caps a single oversized frame but provides **no cumulative backpressure**. A fast
producer (`cat /dev/urandom`) over a slow browser → workerd buffers outbound
unboundedly → the Durable Object hits its **128 MB limit and is OOM-killed**,
dropping the session for everyone.

## Key constraint: no `bufferedAmount`

workerd does **not** implement `WebSocket.bufferedAmount`
([cloudflare/workerd#988](https://github.com/cloudflare/workerd/issues/988), still
open). The repo's `CFWebSocket` stub doesn't declare it and it appears nowhere in
source/vendored runtime. **The DO therefore cannot measure its own outbound
buffer** — a slow consumer can only be detected via an *application-layer
consumption signal (ACKs)*. This is the load-bearing fact: ACKs are required, not
optional.

## Design

### Signal — per-browser ACKs (browser → DO)
New frame `ack {seq}`. The DO tags each outbound data frame with a monotonic
per-browser `seq` and a cumulative byte total; tracks per browser
`sent_bytes` / `acked_bytes`; `inflight = sent_bytes − acked_bytes`. The frontend
ACKs on a cadence (every K frames / T ms, e.g. after writing to xterm), coalesced
— not one ACK per frame.

### Threshold
Per-browser `MAX_INFLIGHT_BYTES` (config). `inflight > MAX` ⇒ congested;
`inflight < LOW_WATERMARK` ⇒ recovered.

### Response — two tiers

**Tier A — producer flow control (lossless; primary case).** When the session has
a single/primary consumer (or *all* consumers are congested), the DO sends a
flow-control **pause** to the worker over the existing control channel
(`push_worker_control`) — a `flow=pause` action **distinct from hijack-pause** so
the two don't interfere. The worker stops reading its PTY (PTYConnector already
supports a paused poll via `_paused`; add a separate `_flow_paused` flag) → the PTY
OS buffer fills → `cat` blocks on `write()`. True end-to-end XOFF, **no loss**.
`flow=resume` on recovery.

**Tier B — per-viewer drop-stale + snapshot resync (multi-consumer fairness;
later).** For a congested *viewer* (so a slow viewer can't stall a fast operator),
stop forwarding raw `term` deltas, keep a **bounded** queue only for non-droppable
control frames (hijack_state/presence — overflow ⇒ close `1013 try-again`). On
recovery send a fresh full **snapshot** (idempotent repaint) then resume deltas.
Source the current screen via a worker `snapshot_request` control frame — do **not**
trust the possibly-stale `last_snapshot`.

### The silent-client trap
A client that never ACKs (old frontend, stuck tab) must **not** XOFF the worker
forever. If no ACK arrives within a grace window, treat that consumer as
best-effort (Tier-B drop or disconnect), never a deadlock. Backward-compat:
non-ACKing clients fall back to a time/count safety cap.

### Why not alternatives
- `bufferedAmount` polling — unavailable (#988).
- DO-side queue + drain — `ws.send()` gives no drain signal, so a server-side
  queue can't know when to drain without ACKs.
- Frame batching (CF's documented mitigation) — reduces context-switch overhead,
  not memory growth.

## Implementation slices

1. **Tier A core (DO):** config thresholds; per-browser inflight state; ingest
   `ack` in `lifecycle.webSocketMessage`; honest seq/bytes tagging replacing the
   `_queue_bytes` reset in `broadcast_to_browsers`; congestion → emit
   `flow=pause`/`resume` to the worker; silent-client grace timeout. Unit-tested at
   100% CF coverage. *(No worker/frontend dependency — emits best-effort control
   that is a no-op until slices 2–3 land.)*
2. **Worker honoring:** `flow=pause/resume` handled by ushell + PTYConnector via a
   dedicated `_flow_paused` flag (separate from hijack `_paused`); `snapshot_request`
   answered. PTYConnector is on the mutation perimeter (Linux gate).
3. **Frontend ACKs:** xterm integration emits coalesced `ack {seq}` frames; applies
   a resync snapshot.
4. **Schemas:** add `ack`, `flow_control`, `snapshot_request` to `bridge/schemas.py`;
   regenerate `frames.schema.json` + `frames.ts`.
5. **Tier B:** per-viewer drop-stale + snapshot resync.

## Config (no magic numbers)
`MAX_INFLIGHT_BYTES`, `LOW_WATERMARK`, ACK cadence, silent-client grace,
`MAX_CONTROL_QUEUE` — in the CF config/defaults, not inline literals.

## Testing
DO unit tests (ack accounting; congested → `flow=pause` emitted; recovery →
resume; silent-client timeout; control-queue overflow → close) at 100% CF;
worker tests (flow-pause halts poll without touching hijack-pause;
`snapshot_request` returns current screen) at 100% + Linux mutation gate;
frontend vitest (ACK cadence, resync repaint); a `real_cf` e2e firehose over a
throttled consumer asserting **no OOM** and a **correct final screen**;
conformance parity for the new frames.

## Sources
- workerd #988 — `WebSocket::bufferedAmount` / backpressure (open):
  https://github.com/cloudflare/workerd/issues/988
- CF Durable Objects — Use WebSockets:
  https://developers.cloudflare.com/durable-objects/best-practices/websockets/
