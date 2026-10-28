# Hijack Protocol Matrix

This matrix defines the backend capability contract consumed by `hijack.js`.

**Label vocabulary** — `served`, `unserved`, `unsupported`, `partial`, `N/A`
and the `Y`/`N` qualifiers are defined once in
[`docs/parity-labels.md`](./parity-labels.md). Use those words as written.

**Security controls that differ by language** (authz webhooks, CF Access, SPA SRI,
human VNC relay) are documented in
[`docs/security-language-parity.md`](./security-language-parity.md) — that doc is
the scope/de-scope source of truth for multi-language security parity.

## TypeScript runtime status

`packages/provide-uterm-ts` is a high-coverage `partial` runtime port, not a
served backend and not yet a third backend column for this matrix. The served
server backends are Python (FastAPI), Go, C#, and Cloudflare. Completed
protocol, policy, hub, connector, gateway, authentication, and configuration
libraries are tested as libraries; only these four shared HTTP capabilities
are currently integrated into its running Node server:

| Capability | TypeScript route |
|---|---|
| `sessions.list` | `GET /api/sessions` |
| `sessions.get` | `GET /api/sessions/{session_id}` |
| `sessions.snapshot` | `GET /api/sessions/{session_id}/snapshot` |
| `sessions.set_mode` | `POST /api/sessions/{session_id}/mode` |

The Node server also exposes health/liveness/readiness probes and REST hijack
lease actions, but it does not yet serve browser or worker WebSockets, the
complete session lifecycle, the hijack events poll, or every shared HTTP
route. Consequently it must not be read as supporting the FastAPI or
Cloudflare columns below.

Before TypeScript joins the Python/Go/C# multi-backend Playwright matrix, it
must serve authenticated `/ws/worker/{worker_id}/term` and
`/ws/browser/{worker_id}/term` flows with hub attachment, hello/control
frames, broadcast, and disconnect cleanup; it must also serve the fixture
lifecycle from session creation and start/attach through status, stop, and
deletion. Those surfaces need focused live integration tests before the
Playwright backend selector and CI matrix are expanded.

## Multi-session fan-out

Fan-out has a stricter security contract than ordinary group access: access to
a group never implies access to the sessions in it. Every send re-resolves each
member and checks the calling principal's current session authorization. A
revoked, deleted, or still-dormant member is reported as failed and receives no
input or observer notification.

The served Python, Go, and C# backends use the configuration key
`fanout_allow_unknown_members`, which defaults to `false`. Setting it to `true`
permits a global administrator to create a group containing dormant IDs, but
it does not weaken send-time authorization. The `unserved` TypeScript route
component instead accepts the default-false `allowUnknownMembers` option; there
is no mounted TypeScript server setting for this surface.

All five REST operations — create, list, delete, send, and grant — require an
authenticated global administrator. Session-scoped admins are rejected before
request parsing or group lookup.

Label definitions: [`docs/parity-labels.md`](./parity-labels.md).

| Capability | Python FastAPI | Go | C# | TypeScript |
|---|:---:|:---:|:---:|:---:|
| Fan-out REST surface served | Y | Y | Y | N — unserved |
| Global admin required for create/list/delete/send/grant | Y | Y | Y | Y — unserved |
| Browser-WS fan-out send served | Y | Y | N — not implemented (REST only) | N — not implemented (REST module only) |
| Reject unknown members by default | Y | Y | Y | Y — unserved |
| Explicit dormant-member opt-in | `fanout_allow_unknown_members` | `fanout_allow_unknown_members` | `fanout_allow_unknown_members` | component option `allowUnknownMembers` — unserved |
| Reauthorize every member on send | Y (REST + browser WS) | Y (REST + browser WS) | Y (REST) | Y — unserved (route/controller module) |
| Reauthorize approval release | Y | N/A (no fan-out approval store) | N/A (no fan-out approval store) | Y — unserved (controller module) |
| Group grant cannot bypass session authz | Y | Y | Y | Y — unserved |
| Configured governance behavior | served — webhook deny/hold/release; errors fail closed | unsupported (501) — deterministic, no input | unsupported (501) — deterministic, no input | unserved — policy-gate module implemented, not mounted |
| Parallel/sequential collection and divergence | Y | Y | Y | Y — unserved |
| Live `fanout.rest.strict` server cell | Y | Y | Y | N — unsupported (not advertised) |

The browser-WS row is the one place where "not implemented" is the honest
answer rather than `unserved`. Python dispatches a `fanout_send` control message
on the browser socket (`bridge/routes/websockets_browser.py`) and Go has
`server/ws_browser_fanout.go`. C# mounts the five fan-out REST routes
(`Server/UtermServer.Fanout.cs`) but its browser-socket dispatcher
(`Server/UtermServer.BridgeWs.cs`) has no `fanout_send` case, and the string
appears nowhere in the C# tree — there is no unmounted module to call
`unserved`, and the surface plainly exists in that architecture, so it is not
`N/A` either. TypeScript is in the same position for a different reason: its
fan-out code is the REST controller module (`src/fanout/controller.ts`), which
no server mounts, and no browser-WS send path was written at all. Both are
roadmap gaps — see
[`feature-roadmap.md`](./feature-roadmap.md) § Multi-Session Fan-Out.

`conformance/live/scenarios/010_fanout_strict_admission.json` executes the
strict-default REST contract across the served Python, Go, and C# backends
with clients from all four languages.
The harness requires the selected client's registered static capabilities and
the running server's announcement to contain `fanout.rest.strict` before it
launches the client. It validates the returned client capabilities again after
the run. A manually selected TypeScript server therefore produces an explicit
`unsupported` cell before any client process is launched.
The richer cases that require mid-scenario authorization mutation, policy
infrastructure, capture lifecycle checks, store concurrency, or deadline
control are defined by `spec/fanout_security_scenarios.json` and executed by
`scripts/run_fanout_security_scenarios.py`. TypeScript deliberately does not
advertise live fan-out until the Node server mounts the route module.

## Hijack control

This table recorded a Cloudflare divergence that had stopped being true: it
advertised `hijack_control=rest` and a `use_rest_hijack_api` refusal for the
three WS hijack frames. The Worker emits `"ws"` from all three of its hello
paths and serves all three frames, and that refusal code exists nowhere in the
tree — the doc was its only occurrence. Pinned now by
`packages/provide-uterm-cloudflare/tests/test_edge_divergence_matrix.py`, so the
next drift fails a test rather than sitting here. `mcp_supported` and
`vnc_supported` are *omitted* from the Worker's hello rather than sent as
`false`; the frame schema defaults them to `false` for a consumer that reads
them positionally.

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `hello.hijack_control` | `ws` | `ws` |
| `hello.hijack_step_supported` | `true` | `true` |
| `hello.mcp_supported` | `true` | key omitted |
| `hello.vnc_supported` | `true` | key omitted |
| Human VNC relay (browser RFB proxy) | `WS …/hijack/{id}/gui/vnc` (authz + RFB filter; upstream dial optional) | N/A |
| WS frame `hijack_request` | served | served |
| WS frame `hijack_release` | served | served |
| WS frame `hijack_step` | served | served |
| REST `/hijack/acquire` | served | served |
| REST `/hijack/{id}/heartbeat` | served | served |
| REST `/hijack/{id}/release` | served | served |
| REST `/hijack/{id}/step` | served | served |
| REST `/hijack/{id}/send` | served | served |
| REST `/hijack/{id}/snapshot` | served | served |
| REST `/hijack/{id}/events` | served | served |

## Session resumption

Opt-in feature. Enabled on FastAPI by passing `resume_store` to `TermHub`; always enabled on the CF backend (SQLite-backed).

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `hello.resume_supported` | `true` when store configured, else absent | `true` always |
| `hello.resume_token` | opaque token (256-bit, urlsafe) | opaque token (256-bit, urlsafe) |
| `hello.resumed` | `true` on successful resume | `true` on successful resume |
| WS frame `{"type":"resume","token":"…"}` | served (first message after connect) | served (any browser message) |
| Token TTL | configurable via `resume_ttl_s` (default 300s) | configurable via `resume_ttl_s` (default 300s) |
| Token storage | `InMemoryResumeStore` (default) or pluggable | DO SQLite `resume_tokens` table |
| Token lifetime after disconnect | preserved until TTL | preserved until TTL |
| Invalid/expired token behavior | silently ignored, fresh session stands | silently ignored, fresh session stands |
| Hijack ownership recovery | yes, if lease still active and no new owner | yes, if lease still active and no new owner |
| Browser storage | `sessionStorage` keyed by `uterm_resume_{worker_id}` | same |

## Client behavior contract

- The client must key behavior on `hello.hijack_control` (or `hello.capabilities.hijack_control`).
- The client must not assume backend type by URL or deployment.
- Unsupported WS control paths must degrade to REST when `hijack_control=rest`.
- If `hello.resume_supported` is `true` and a stored token exists, the client must send `{"type":"resume","token":"…"}` as its first message after connect.
- The client must update its stored token on every hello (initial and resumed) — tokens are rotated on each resume.
- FastAPI resume tokens are opaque session handles. By default they restore the
  prior browser role from the token; consumers that need identity-aware resume
  checks must provide `on_resume` validation when constructing `TermHub`.

### Transport close (client side)

No wire bytes change here: this is how every client port *reports* the end of a
connection. The contract is `provide.uterm.transport_close` (Python), mirrored
by `TransportClose` in TypeScript, Go and C#, and pinned for all four by
`close_cases` in `spec/behavior_vectors.json` (generated from the Python
reference by `scripts/generate_behavior_vectors.py`).

| Event | Initiator | Code / reason |
|---|---|---|
| WebSocket close frame received first (or only) | `remote` | from the received frame |
| WebSocket close frame sent first (or only), e.g. keepalive timeout | `local` | from the sent frame (`1011 keepalive ping timeout`) |
| WebSocket ended with no close frame | `unknown` | none |
| Telnet EOF, or peer reset on receive/send | `remote` | none |
| Telnet broken pipe | `unknown` | none |
| Telnet receive-buffer cap exceeded | `local` | reason `receive buffer exceeded` |
| Chaos injected disconnect | `unknown` | reason `injected disconnect` |

`summary()` is `"<initiator> close"`, then the code and reason when present,
then `" (<detail>)"` when there is a detail; a code of `0` is a code, not an
absent one. Port gaps are skipped by name in each port's vector test: C# has no
chaos transport and no telnet receive-buffer cap.

Each port's reconnecting wrapper keeps the close that triggered its last
reconnect, or the final attempt when the retries ran out: Python
`ReconnectingSession.last_close`, TypeScript `lastClose`, Go
`ReconnectingTransport.LastClose()` and C# `ReconnectingTransport.LastClose`.
It is set from a retried failure that carried a typed close; the session-level
wrappers (Python, TypeScript) also take the dropped session's own close info
before closing it. An untyped drop leaves the previous value in place.
`spec/uterm-api.yaml` requires `last_close` in Python, Go and C#.

## Tunnel protocol

Binary multiplexed WebSocket framing for terminal sharing, TCP forwarding, and HTTP inspection.

### Wire format

`[1 byte channel][1 byte flags][N bytes payload]` per binary WebSocket message.

| Channel | Name | Payload | CLI command |
|---------|------|---------|-------------|
| `0x00` | Control | JSON: `open`, `resize`, `close`, `error` | all |
| `0x01` | Terminal | Raw PTY bytes or log lines | `uterm share`, `uterm inspect` |
| `0x02` | TCP | Raw TCP bytes | `uterm tunnel` |
| `0x03` | HTTP | Structured JSON: `http_req`, `http_res` | `uterm inspect` |

Flags: `0x00` = data, `0x01` = EOF (half-close).

### Tunnel endpoints

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Agent endpoint | `WSS /tunnel/{worker_id}` — registers a *tunnel* worker | `WSS /tunnel/{tunnel_id}` (via DO) — registers an *ordinary* worker socket |
| Browser endpoint | `WSS /ws/browser/{id}/term` | same |
| `POST /api/tunnels` | served | served |
| `DELETE /api/tunnels/{id}/tokens` | served (revocation) | served (revocation) |
| `POST /api/tunnels/{id}/tokens/rotate` | served (rotation) | served (rotation) |
| `POST /api/sessions/{id}/viewers/revoke` | served: refuses every `share:{id}:...` viewer token issued up to now and closes live share-viewer sockets with `4410 viewer access revoked` (also Go, C#; TS refuses reads, binds no socket). An optional body `{"subject": "share:{id}:..."}` does the same for that one subject only: the session's other viewers keep watching and a token minted for the subject later is admitted. No body, a blank one, `null` or `{}` revokes the whole session, which also drops that session's subject cutoffs at or before it. Any other body, one past 4 KiB, or a subject that is not a string or spells a lone surrogate is `422 invalid request body`; a subject without the `share:{id}:` prefix, with nothing after it, or past 512 UTF-8 bytes is `422 subject must be a share subject of this session`. A new subject is refused with `429` when the session already holds 100 subject cutoffs or all sessions together hold 10 000. A subject revoke's answer adds `"subject"` (the subject applied); a whole-session answer has no `subject` key. Servers that support the body list `viewers.revoke.subject` in `/api/health` `features`; an older server ignores the body and revokes every viewer, so check before sending one. The browser elements treat 4410 as final and do not reconnect (`uterm:close` reports it) | not served (no share-viewer grants) |
| `GET /api/previous-sessions` | served (also Go; not C#, TS): `{"sessions": [{"id", "display_name", "owner", "ended_at", "cols", "rows"}]}`, newest first (ties by id), only the ended PAM capture sessions the caller may read. History is read by fewer than the live session was: its owner, an admin, and an operator unless it was private, admin and operator being the caller's standing, asked of the authorization provider once a request rather than once a record (admin: the provider's `is_admin` with `session.control.hijack`, the take-over capability the live read's admin role requires, so a webhook provider decides it; an admin scoped to a session is admin of only that one; operator: the operator role, as the live read's role resolution reads it); nobody else, not even for a public session, and never a share viewer (a `share:` subject, a watch or a tunnel share), whatever its token: a grant is for the live stream, and its revocations are not kept across a restart. Each must also pass `can_read_session` on the definition it ended with. Empty when `session_persistence.previous_keep_days` is 0. Listed in `/api/health` `features` as `sessions.previous` when on | not served (no capture sessions) |
| `GET /api/previous-sessions/{id}` | served (also Go): the listing's fields plus `"screen"` (the last `ansi_screen()`, rows joined with LF) and `"scrollback"` (plain-text rows that scrolled off above it, oldest first, at most `previous_scrollback_lines`), both through the output-redaction policy the live screen is redacted by, its rules fetched once a read; a policy that cannot answer: `503 output policy unavailable`, never the unredacted screen. Unknown, or its file gone since: `404 unknown previous session: {id}`; not readable by the caller (by the listing's rule): `403 insufficient privileges`, as the live session's read answers; a path-unsafe id: `422` | not served |
| Share URL (`?invite=...`) | `/s/{id}` → set HttpOnly cookie, 302 clean redirect | `/s/{id}` → set HttpOnly cookie, 302 clean redirect |
| Inspect view | `/app/inspect/{id}` | `/app/inspect/{id}` |

The Cloudflare agent endpoint is mounted, but mounting it is not the same as
serving the multiplexed transport above. `entry/registry.py` matches
`/tunnel/{id}` and proxies it to the Durable Object, which upgrades it with the
ordinary worker socket role (`do/session_runtime/fetch.py`). Inbound frames are
decoded by `api/tunnel_routes.py`, which handles the control (`0x00`) and
HTTP-inspect (`0x03`) channels and folds every remaining channel into the
terminal channel — so TCP (`0x02`) is not demultiplexed — and because no
`is_tunnel_worker` state exists at the edge the browser-to-agent direction is
never re-encoded into channel framing. The tunnel-transport fragmentation
scenario is `unserved` on Cloudflare for that reason, recorded in
[`security-language-parity.md`](./security-language-parity.md#session-lifecycle-security)
and pinned as row 11 of
[`cloudflare-divergence-matrix.md`](./cloudflare-divergence-matrix.md).

### Tunnel auth

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Agent auth | Global `worker_bearer_token` OR per-session `worker_token` | same |
| Share token | `uterm_tunnel_{id}` cookie only after invite bootstrap | `uterm_tunnel_{id}` cookie only after invite bootstrap |
| Control token | `uterm_tunnel_{id}` cookie only after invite bootstrap | `uterm_tunnel_{id}` cookie only after invite bootstrap |
| Token TTL | Default 1h, configurable via `TunnelConfig.token_ttl_s` | Default 1h, configurable via `TUNNEL_TOKEN_TTL_S` env var |
| Token revocation | `DELETE /api/tunnels/{id}/tokens` | `DELETE /api/tunnels/{id}/tokens` |
| Token rotation | `POST /api/tunnels/{id}/tokens/rotate` | `POST /api/tunnels/{id}/tokens/rotate` |
| IP binding | Optional (`TunnelConfig.ip_binding`) | Optional (`TUNNEL_IP_BINDING` env var) |
| Timing-safe compare | `secrets.compare_digest()` | same |
| Enumeration prevention | 404 for both "not found" and "invalid token" | same |

### HTTP inspection (channel 0x03)

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Channel 0x03 broadcast | `hub.broadcast()` + `hub.append_event()` | `runtime.broadcast_worker_frame()` |
| HTTP req/res JSON | Parsed, tagged `_channel: "http"`, broadcast to browsers | same |
| Invalid JSON handling | Logged as warning, dropped | same |
| Body < 256KB | Included as `body_b64` (base64) | same (agent-side encoding) |
| Body > 256KB | `body_truncated: true`, no `body_b64` | same |
| Binary content | `body_binary: true`, no `body_b64` | same |
| Inspect view | `/app/inspect/{id}` — live request list + detail | same |

## Capture wire

What a `pty_capture` session reads from its Unix socket: traffic captured from
a program by `libuterm_capture` (the LD_PRELOAD/DYLD shim), plus what the pty's
owner adds. Owner-only socket (`0600`, bound under umask `0177`) unless
`[pam] capture_socket_mode` says otherwise (below).

### Capture socket mode

`[pam] capture_socket_mode` (Python and Go; TypeScript and C# accept and check
the key, and bind nothing) is the permission every capture socket the server
binds for a `pty_capture` session gets: the ones PAM asks for in
`capture_socket_dir`, the ones a sessions API create names in its
`connector_config.socket_path` (a play mirror beside them, say), and the ones
re-bound for a session kept across a restart. Connecting to a Unix socket needs
write permission on it, so the mode says who may produce into the socket, and
so read and type into the terminal.

- A TOML integer, `0o600` (the default: only the server's user) or `0o660`
  (the socket directory's group too). It is for a server that runs as a user
  of its own while the producers (the shim, a session supervisor) run as
  another. The group comes from the directory, made setgid, so the server
  needs no chown and no group name.
- Anything else is refused at load, naming the key: another mode
  `pam.capture_socket_mode must be 0o600 or 0o660, got: 0o644` (written as
  Python's `oct()` writes it, `-0o1` for a negative one), and a value that is
  not an integer (a string such as `"0o660"` or `"432"`, a float, a bool)
  `pam.capture_socket_mode must be an integer: 0o600 or 0o660`. Python prefixes
  its config errors `Value error, `, as for every field. JavaScript has one
  number type, so TypeScript reads `432.0` as 432 and accepts it.
- The socket is bound under the umask that leaves exactly the mode (`0177`
  for `0600`, `0117` for `0660`) and chmod'd to it after, as before.
- The mode is the server's, never the session's: the runtime adds it to the
  config a capture connector is given (`socket_mode`), over anything the
  stored `connector_config` says, and it is never stored, so a kept session's
  file does not carry it. A capture connector built directly accepts
  `socket_mode` (`0o600` or `0o660`, refused otherwise as
  `capture socket mode must be ...`); a different mode for a running one is
  left for a restart, as a different socket is.
- Not the PAM notify socket: `pam_uterm` writes that one as root, and it stays
  owner-only.
- `/api/health` lists `capture.socket_mode` in `features` when the mode is
  `0o660` (Python and Go), so a producer running as another user can tell
  that it will be able to connect.

`[1 byte channel][4 bytes length, big-endian][N bytes payload]`, `N` at most
16 MiB; a larger claim drops the connection before the body is read.

| Channel | Name | Payload | Sent by | Python server | Go server |
|---------|------|---------|---------|---------------|-----------|
| `0x01` | stdout | Raw bytes the program wrote | shim | Screen (emulated), streamed as `term` | Screen (emulated by the hosted `TransportSession`), streamed as `term` |
| `0x02` | stdin | Raw bytes the program read | shim | Counted for analysis | Counted for analysis |
| `0x03` | connect | Peer address, UTF-8 | shim | Logged for analysis (last 100) | Logged for analysis (last 100) |
| `0x04` | stats | Shim writer counters, ASCII `key=value` | shim | Reported in analysis | Ignored |
| `0x05` | geometry | `>HH` cols, rows; each 1-1000 | pty owner (supervisor); the LD_PRELOAD shim too, from `TIOCGWINSZ` on connect and at most once per 250 ms on the write path, when the size changed | `CaptureConnector` resizes as `reconfigure` does (re-emulates output since the last clear), and `poll_messages` returns a `snapshot`, which the runtime sends to the hub | `pty.CaptureConnector` takes the size and returns a `snapshot`; `CaptureTransport` turns it into `TransportSession.Resize` (the session's emulator rebuilt at the new size from output since the last clear), and the worker bridge sends the hub a `snapshot` unasked |
| `0x06` | input | UTF-8 absolute path; the producer's keystroke-socket path. Empty payload withdraws | A producer that can deliver keystrokes (a supervisor); never the shim, which has no keystroke socket to name | `CaptureConnector.poll_messages` validates the path convention (`.sock` → `.stdin.sock`), the peer (`SO_PEERCRED` pid must be the login's pid or a descendant, walking `/proc` parent links), then sets `stdin_socket_path` in place, closing any open writer. An admin-set path in the config wins over an announcement | `pty.CaptureConnector.PollMessages` mirrors the Python logic: convention check, peer-pid check, `stdinSocketPath` set in place. `configStdinSocketPath` field tracks the config-set path so an admin PATCH wins |

- A reader ignores a channel it does not know, so a producer can send a newer
  channel to an older connector unconditionally.
- A geometry payload that is not exactly four bytes, or names a side outside
  1-1000, is ignored and the stream continues (its length kept the framing
  intact). A geometry naming the current size emits nothing; of several
  read in one poll, the last valid one is applied, once, and painted once.
- Either way the hub broadcasts the worker's `snapshot` to every viewer and
  watcher; that is how they learn the size. A restarted Go session comes back
  at the last geometry, not the configured size.
- An admin `PATCH` that carries the size the config already named (a stored
  definition replaced for another reason) leaves a geometry-set size alone in
  Python; one naming a new size applies. The Go capture connector has no live
  reconfigure. The stored definition keeps the configured size either way.
- Geometry is state, but it shares the capture queue (4096 frames, oldest
  dropped first): under backpressure a geometry frame can be lost, so a
  producer resends it on its next resize, or periodically.
- After a geometry frame the connector's snapshots report the new `cols` and
  `rows`. `uterm-session` dispatches `uterm:geometry` (`detail: {cols, rows}`)
  when a snapshot's size (positive whole numbers only) differs from the last it
  saw since its terminal was made, after that terminal exists and before it
  paints the snapshot, so a page that sizes its own terminal can follow.
- Declared in `provide.uterm.pty.capture` (Python), `pty/capture.go` (Go),
  `src/pty/capture.ts` (TypeScript, framing only: no connector) and
  `native/capture/capture.c` (the shim). `scripts/check_protocol_drift.py`
  requires all four to declare the same channels with the same bytes.
- `capture_geometry_frame` in `spec/behavior_vectors.json` (generated from the
  Python reference) holds the Python and Go connectors and the TypeScript reader
  to one reading of each frame.
- `capture_input_frame` in `spec/behavior_vectors.json` holds `parse_input_path`
  (the parser) to the same shapes across ports: accepted absolute path, root-only,
  empty (withdraw), relative (rejected), non-UTF-8 (rejected), over-max-len
  (rejected). The connector's convention check and peer-pid check are tested
  separately; the vectors test the parser only.
- The input channel's path convention keeps the path out of a producer's
  choosing: the connector accepts only the capture socket's path with `.sock`
  replaced by `.stdin.sock` (Black Betty's `remote_input_socket_from_capture_socket`).
  A future relaxed rule (any socket in the capture directory) needs no wire change.
- The peer check (`SO_PEERCRED` pid must be the login's pid or a descendant,
  walking `/proc/<pid>/stat` parent links, at most 64 steps) is Linux-only.
  On macOS (no `SO_PEERCRED`), `0x06` is refused with
  `capture_input_refused reason=peer_unverifiable`; an admin-set
  `stdin_socket_path` still works there.
- An empty `0x06` payload withdraws the path: the connector clears
  `stdin_socket_path` and closes any open writer, as `reconfigure` does when
  `stdin_socket_path` is cleared. A producer announces the path on every
  connect, so the first `0x06` wins and subsequent ones are idempotent.
- C# has no capture connector.

### Capture sessions across a restart

`[session_persistence]` (Python and Go; TypeScript and C# accept and bound the
keys, with the same refusals, and keep nothing). A PAM capture session, one
whose id is `pam-<user>-capture-<pid>-<start ticks>` (`<user>` ASCII
`[A-Za-z0-9_-]`, the id at most 200 bytes, pid and ticks int64), is kept in one
JSON file under `state_dir`: `live/<id>.json` while it exists,
`previous/<id>.json` once it has ended.

`state_dir` is named outright, never derived, so two servers do not share one
by accident: required when `persist_capture` is on or `previous_keep_days` is
above 0 (`session_persistence.state_dir is required when
session_persistence.persist_capture is on or session_persistence.previous_keep_days
is above 0`), and absolute (`session_persistence.state_dir must be an absolute
path, got: <path>`, the path as `pathlib` writes it). At startup it is created
`0700` if missing (its parent must exist), and a server whose `state_dir` is
not a `0700` directory this user owns does not start:
`session_persistence.state_dir <path>: mode 0755, not 0700`, `...: not a
directory this user owns`, or `...: cannot create: ENOENT`. A `live/` or
`previous/` directory that will not do the same way is skipped with
`capture_state_dir_ignored` (`error=` the reason, or `cannot list: EACCES`),
never fatal. Both ports write and read the same
format (`packages/provide-uterm-go/capturestate/testdata/capture_record_golden.json`
holds the reference's files and its refusals):

```json
{"format": 1, "kind": "live",
 "session": {"session_id": "...", "connector_type": "pty_capture", "...": "the whole definition"},
 "recording_instance": "<hex>", "login": {"pid": 4242, "start_ticks": 777},
 "screen": {"ansi": "<ansi_screen()>", "cols": 80, "rows": 24},
 "scrollback": ["<plain text>"], "saved_at": 1760875200.5}
```

A previous record has `"kind": "previous"` and `"ended_at"`; `screen` is
`null` until one was taken. Both ports judge a file on its JSON, so they accept
and refuse the same files.

- `format` is the compatibility contract: a newer one than the build reads is
  refused (`newer format: 2`), one below 1 or not an integer is `unknown
  format`. A change an older build could not read safely, beyond a definition
  key added or removed, is a new format.
- Within a format, builds may differ in the definition's keys, since an
  upgrade or a downgrade restarts on the files the other build wrote. A key
  this build does not know is ignored, never folded into `connector_config`
  and never matched by case; it is noted once a file at load, as
  `capture_state_keys_ignored path=... keys=a,b` at info, not a warning. A
  missing key that has a default takes the model's (`display_name` the id,
  `input_mode` `open`, `auto_start` true, `tags` `[]`, `recording_enabled`
  null, `created_at` now, `ephemeral` and `presence` false,
  `auto_transfer_idle_s` 30, `keystroke_queue` `display`). `session_id`,
  `connector_type`, `connector_config`, `owner` and `visibility` are never
  defaulted, since a default would change what the session connects to or who
  reads it: without one, `bad session: <key>`.
- Every key present is of its JSON type (`bad session: <key>`); `login` and
  `screen` have exactly their keys, with int64 numbers; `scrollback` is a list
  of strings; `saved_at` and `ended_at` are finite and not negative.

Bytes that are not one JSON object (not UTF-8, `NaN`, nested past the parser)
are `not an object`. Any other refusal is a reason with no file content in it
(`session id is not the file's name`, `owned by another user`, `not a regular
file`, `too large` past 16 MiB, `cannot open: ELOOP` for a symlink, `cannot
read: EIO` on a failing disk, by errno name in both ports), logged as
`capture_state_file_ignored` and skipped; a previous session's read answers
it 404, as a file gone since it was listed. The file is opened without
following a symlink and without blocking, so a FIFO is refused rather than
waited on.

- With `persist_capture`, the definition is written on create, update and mode
  change (in Go outside the registry's and the store's locks, in order per
  session); the screen on the recording's settle trigger, as a second sink that
  runs with no recording and past its quota, at most once a second and only
  when it changed; and once more as the session stops.
- At startup, after the control plane's migration and before sessions
  auto-start, each live file is re-created under its id, definition and
  recording instance (its recording is kept), and its first connector is
  painted with the saved screen, CRLF for LF, which also stands in for the
  output since the last clear on a resize. The screen stays pending until a
  start succeeds, so a first start that fails leaves it for the retry. The
  scrollback kept so far carries over every start, a stop and start in this
  process as much as a restart. Its socket must pass the
  confinement PAM's open applies, and its login's pid must still report the
  start ticks the id names; otherwise it ends.
- With `previous_keep_days` above 0, a session that ends (PAM close, `DELETE`,
  or found gone at startup, dated by its last save) is kept with its last
  screen and up to `previous_scrollback_lines` rows of scrollback, the rows
  that scrolled off the top of the main screen (none from the alternate
  screen, or from a region that starts lower down). One that ended longer ago
  than its days (a login gone during a long downtime) is not written. Pruned
  past its days at startup and hourly, and past 20 per owner, the newest kept;
  a definition with no owner counts against the PAM user its id names. With 0
  the file is deleted.
- A capture socket takes over only a stale socket at its path: both ports
  probe it first (`lstat`, then a connect, in Python on a thread so the loop
  is not held) and remove it only when the connect is refused. One something still listens on, another session's, is left and
  the bind fails (`capture socket is in use: <path>` in Python, `EADDRINUSE`),
  as is one that does not say (another error, or no answer in time); a file
  that is not a socket is left for the bind to refuse.
- Go's session create ignores `auto_start`, so a Go PAM capture session (kept
  or not) runs once a client starts it, as before.

## Login record (adoption)

When uterm restarts, a live SSH login is still alive but uterm's in-memory
session registry is gone. `restore_capture_sessions` re-creates sessions uterm
persisted itself; the adoption scan covers logins uterm never heard of — the
ones that opened while uterm was down.

### The login record file

In `mode=capture`, before notifying, `pam_uterm` writes the open event's JSON
line (the same bytes, `start_ticks` and `ssh_auth_info` included) to:

```
<cap_dir>/uterm-cap-<pid>.login
```

Created as root with `O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`, mode `0640`, its
group inherited from the setgid capture directory. A file already at that path
(a pid reused after a crash) is unlinked first. `pam_sm_close_session` unlinks
it. Records live in `/run` and vanish at reboot.

The `<pid>` is the PAM process's pid (the process that ran PAM). The JSON line
is the same one sent to the notify socket — same fields, same escaping, same
newline terminator.

### The startup scan

At startup, after `restore_capture_sessions` and before sessions auto-start,
uterm lists `uterm-cap-*.login` in the capture directory
(`pam.capture_socket_dir`, else the notify socket's directory) when
`pam.mode = "capture"`. Each record is trusted no further than a PAM event,
and less than a file uterm wrote itself:

1. `lstat`: a regular file, owned by uid 0, not group- or world-writable, at
   most 16 KiB. A capture-group member can create files in the directory but
   cannot create one owned by root.
2. Parsed as a `PamEvent` with `event: open`, `mode: capture`; its
   `capture_socket` must be exactly `<dir>/uterm-cap-<pid>.sock` for the pid in
   its name and must be confined to the capture directory.
3. Its `start_ticks` must be present and equal what `/proc/<pid>/stat` reports
   now. No start ticks, no adoption: a random-tag id cannot be proven.
4. Its id (`pam-<user>-capture-<pid>-<start_ticks>`) must not already be in the
   registry. A session `persist_capture` restored is left as restored, with its
   screen, owner and recording.

A record that passes is handled as PAM's open would have been: the same
`_on_open` path, so the session is created with decision 1's owner (the hook is
called with `adopted: true`). A record that fails is logged once at debug (a
dead login's leftover is normal after an sshd crash) or at warning (wrong
owner, wrong shape, socket not confined) and left alone: uterm cannot remove a
root-owned file from a sticky directory, and should not.

The scan runs once, at startup. Adoption does not require `persist_capture`:
without it, adoption alone re-creates every live login's session after a
restart, without its screen. The `sessions.capture.adopt` feature in
`/api/health` advertises it is in effect.

### Who reconnects

An adopted session binds the capture socket; a producer has to connect to it.
A supervisor retries until it can. The shim connects once, at process start, so
a shim-captured login that opened while uterm was down stays uncaptured:
adoption makes its session, and the session shows nothing until the login ends.
That is unchanged from today — the shim has no reconnect logic (C9).

## Health checks

Unauthenticated endpoints for load balancers, orchestrators, and monitoring.

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `GET /api/health` | served (no auth) | served (no auth) |
| Response: `status` | `"ok"` or `"unavailable"` | `"ok"` (via `ok: true`) |
| Response: `version` | package version string | not included |
| Response: `uptime_s` | seconds since server start | not included |
| Response: `active_sessions` | count from session registry | not included |
| Response: `control_plane_backend` | `"memory"` or `"sqlite"` | not included |
| Response: `features` | `["viewers.revoke.subject"]` when ready (also Go, C#, TS): optional behaviour a client cannot otherwise detect; absent from the 503 answers. Python and Go add `sessions.capture.persist` when `session_persistence.persist_capture` is on, `sessions.previous` when `session_persistence.previous_keep_days` is above 0, and `capture.socket_mode` when `pam.capture_socket_mode` is `0o660` | not included |
| `GET /healthz` | served (minimal, no deps) | unsupported |
| Auth required | no | no |

## Security headers

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| `security.mode` | `"strict"` / `"dev"` (SecurityConfig) | `SECURITY_MODE` env var |
| Content-Security-Policy | strict: full CSP; dev: not set | same |
| Strict-Transport-Security | strict: `max-age=63072000; includeSubDomains`; dev: not set | same |
| X-Frame-Options | strict: `DENY`; dev: not set | same |
| X-Content-Type-Options | always `nosniff` | same |
| Referrer-Policy | strict: `strict-origin-when-cross-origin`; dev: not set | same |
| Permissions-Policy | strict: `camera=(), microphone=(), geolocation=()`; dev: not set | same |
| Per-header override | config field (None=default, ""=suppress, "value"=custom) | env var (same semantics) |
| SRI on CDN assets | `integrity` + `crossorigin` on all jsdelivr script/link tags | same |
| WebSocket 101 bypass | headers not applied to WS upgrades | same |

## DeckMux (collaborative presence)

Real-time collaborative presence for terminal sessions. Enabled per session with `presence: true`.

| Capability | FastAPI backend | Cloudflare backend |
|---|---|---|
| Session config: `presence` | `SessionDefinition.presence` | KV session entry |
| `presence_update` relay | TermHub broadcast | DO broadcast |
| `presence_sync` on join | sent from hub mixin | sent from DO |
| `presence_leave` on disconnect | sent from hub mixin | sent from DO |
| `control_request` / `control_transfer` | via hijack lease system | via DO lease state |
| Auto-transfer (idle owner) | background check in hub | event-driven in DO |
| Keystroke queue | in-memory buffer | in-memory buffer |
| Hibernation recovery | N/A (always running) | ephemeral re-announce |
| Identity (JWT users) | from principal claims | from JWT claims |
| Identity (anonymous) | deterministic adjective+animal | same |
| Edge indicators | frontend-only | same |
| Name labels toggle | frontend-only | same |

### DeckMux message types

| Direction | Type | Payload |
|---|---|---|
| Browser -> Server | `presence_update` | `scroll_line`, `scroll_range`, `selection`, `pin`, `typing` |
| Browser -> Server | `queued_input` | `keys` (buffered keystrokes from non-owner) |
| Browser -> Server | `control_request` | `target` (user to request control from) |
| Server -> Browser | `presence_update` | `user_id`, `name`, `color`, `role`, scroll/selection/pin state |
| Server -> Browser | `presence_sync` | `users` (full state array), `config` |
| Server -> Browser | `presence_leave` | `user_id` |
| Server -> Browser | `control_transfer` | `from_user_id`, `to_user_id`, `reason`, `queued_keys` |
| Server -> Browser | `auto_transfer_warning` | `seconds_remaining` |

These are the seven message types defined in
`packages/provide-uterm/src/provide/uterm/deckmux/_protocol.py`. Control is
*requested* via a `control_request` and *granted* via a single
`control_transfer` whose `reason` is one of `handover`, `auto_idle`,
`admin_takeover`, or `lease_expired`; there are no separate
handover/deny/notification/denied message types — those flows are handled by the
hijack lease system. `auto_transfer_warning` is the only idle-warning
Server -> Browser message.

All messages use the existing control channel (DLE+STX JSON framing). 200ms client-side debounce on presence updates. The deckmux module is part of the `provide-uterm` core package.

## Accuracy note

This document describes the intended public contract. It does not mean every
backend edge case is perfectly identical today. In particular, verify auth and
lease-validation behavior against current tests before treating the two
backends as interchangeable for security-sensitive flows.
