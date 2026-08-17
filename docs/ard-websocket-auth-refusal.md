# ARD: WebSocket Authentication and the Shape of a Refusal

## Status

Accepted

## Problem

Three server ports — Python (the reference), Go, and C# — each expose the same
two WebSocket surfaces:

- `/ws/browser/{worker_id}/term` — viewers and operators.
- `/ws/worker/{worker_id}/term` — the session's worker. The **privileged** half:
  it feeds terminal output to every viewer and drives session state.

Each port authenticated these, and each refused differently. The divergence was
not visible in any test, and one of the shapes was actively misleading.

Measured before this work, with a default-posture server and no credentials:

| port | browser | worker |
|---|---|---|
| Python | 403 pre-upgrade | 403 pre-upgrade |
| C# | **accepted** | 401 pre-upgrade |
| Go | 1008 post-upgrade | 1008 post-upgrade |

Two separate defects sat in that table.

**C# admitted anonymous browser sockets.** The handler authenticated, discarded
the answer, and asked only whether the principal could *read* the session.
`CanReadSession` returns true for visibility `"public"`, which is the shipped
default on a server that creates a session at startup. The hostile-client burst
probe measured 200 of 200 unauthenticated connects accepted, each able to send
input frames into the terminal, while Python and Go refused all 200. The port
already had the right helper — `RequireAuthenticated`, documented as "Go/Python
`require_authenticated` parity: anonymous principal → 401" — it simply was not
used on that path.

**Go refused after completing the handshake.** An unauthenticated client saw a
*successful* upgrade followed by a 1008 close. To anything that stops at
`connect()`, that is indistinguishable from being let in. This is not a
hypothetical failure mode: a probe written during this investigation reported a
Go auth bypass that did not exist, purely because it stopped at the handshake.
The stated justification was "matching the Python accept-before-close ordering",
and Python does not do that — Python raises its 1008 inside a dependency and
Starlette denies the handshake *before* accept, which is why it measures 403.
The comment described an intent, not the behaviour it claimed to mirror.

## Decision

**Every port refuses before the upgrade**, with the status distinguishing the
two failures:

- `401` — no credentials. Authentication failed.
- `403` — credentials accepted, access not permitted. Authorization failed.

Refusing pre-upgrade costs no upgrade, leaves no half-open socket, and cannot be
mistaken for acceptance by a client that only checks whether `connect()`
succeeded. Keeping 401 and 403 distinct lets a client tell "log in" from "you
may not", instead of receiving one indistinguishable close code for both.

## Current Behavior

Measured after the change, both sockets, all three ports:

| port | unauthenticated | unauthorized |
|---|---|---|
| Python | 401 pre-upgrade | 403 pre-upgrade |
| Go | 401 pre-upgrade | 403 pre-upgrade |
| C# | 401 pre-upgrade | 403 pre-upgrade |

The pre-upgrade rule and the 401/403 split now hold on every port.

### How Python emits 401, given that Starlette hardcodes 403

Starlette's unaided WebSocket refusal is `close()` before `accept()`, which every
ASGI server reports as a hardcoded **403** — an authorization answer to an
authentication failure. Emitting 401 needs the ASGI websocket-denial-response
extension, which Starlette exposes as `WebSocket.send_denial_response`.

`app/ws_denial.py` carries it: a `WebSocketAuthDenied` exception, an exception
handler that answers with the chosen status, and a fallback to the old
close-before-accept for any ASGI server that does not advertise the extension.
The refusal body is `{"detail": ...}`, matching this server's own HTTP refusals
and Go's `detailError`, and a 401 carries `WWW-Authenticate: Bearer` as RFC 7235
requires. Measured against real uvicorn on the default implementation: both
sockets answer 401, and a burst of 25 unauthenticated connects answers 401
25 times.

The extension delivers the response correctly on every uvicorn implementation.
What is not correct is what uvicorn does afterwards: the sansio implementation
sets `initial_response` for a denial but never sets `handshake_complete`, and its
`run_asgi` completion check tests only the latter
(`websockets_sansio_impl.py:416`), so a **valid** refusal logs
`ERROR: ASGI callable returned without completing handshake.` Two adjacent
branches in the same file already test both (`:425`, `:438`), as does
`wsproto_impl.py:349`.

The follow-on `send_500_response()` is a **no-op** — it returns early on
`initial_response`, so no 500 ever reaches the client and nothing on the wire is
affected. (An earlier revision of this document claimed a 500 was attempted;
that was wrong, and it was the main reason the 401 was previously abandoned.)
The whole defect is one spurious log line per refusal. That still matters,
because on an authenticated endpoint refusal is the normal case, so a connection
flood would amplify into an equal flood of ERROR records.

`install_ws_denial_support` therefore installs a `logging.Filter` on
`uvicorn.error` that drops exactly that message, and only for connections this
server actually denied. The scoping is a ContextVar: uvicorn awaits the ASGI app
and logs from the same task, so a value set inside the app is still visible to
the filter. A genuine incomplete handshake — a real bug — still logs, because
nothing set the flag for it. Both halves are measured: a denied connection
observes `True`, a deliberately unfinished handshake observes `False`; and with
the filter removed the same run emits 27 ERROR lines where the shipped code
emits 0.

This is why the fix is a filter and not `--ws wsproto`, which was the other
candidate:

| `--ws` | denial behaviour |
|---|---|
| `auto` (the default) | 401 delivered; one spurious ERROR (filtered) |
| `websockets-sansio` | 401 delivered; one spurious ERROR (filtered) |
| `wsproto` | clean |
| `websockets` (legacy) | clean, but **deprecated** — uvicorn warns it will point at the sansio implementation |

Pinning `wsproto` would mean declaring a dependency that is currently
**transitive only** (zero mentions in any `pyproject.toml`), and adopting a
different WebSocket transport for every session this server carries, to fix a log
line. It would also not hold where it matters: the pin would live in `cli.py`,
but the app is launched other ways — `uvicorn.Server(uvicorn.Config(app, ...))`
in the TypeScript golden generators and in this package's own test conftest —
so the status code would have depended on how the server was started. The filter
is installed by the application factory, so it holds on every launch path and on
every implementation.

A standalone reproducer and a proposed one-line patch are drafted at
`../uvicorn-websocket-denial-response-issue.md`, outside this repo. When that
lands, the filter becomes dead weight and can be deleted; nothing else changes.

Consumers are unaffected either way: `runtime_helpers.py` classifies `401`,
`403`, and `404` alike as permanent, so a worker that is refused does not retry
against any port.

## UTERM_TEST_MODE

Setting `UTERM_TEST_MODE=1` mints an admin principal for WebSockets, skipping
authentication and authorization. It exists for the multi-backend Playwright
suite. Every port carries the same comment — "never default-on; production
servers must not set this env" — and that comment was the entire safeguard: no
build-config gate, no loopback requirement, no config opt-in.

It is not set in any Dockerfile, compose file, or other shipped artifact — only
in CI steps and the demo-recording scripts. That property is worth preserving and
worth checking when it changes.

All three ports now **print a warning at startup** when the variable is set, in
identical wording so the string greps across logs from any backend. Behaviour is
unchanged; the point is that a server running with the auth gate open is no
longer indistinguishable from one that is not.

One divergence remains here too, and is **not** resolved:

| port | scope of the bypass |
|---|---|
| Go, C# | browser socket only; the worker socket still requires its bearer token |
| Python | **every** websocket, including `/ws/worker/*` |

Python's test-mode branch lives in `_require_authenticated`, matches
`scope["type"] == "websocket"` with no path filter, and returns *before* the
`/ws/worker/` bearer-token branch. With the variable set, a Python server accepts
an unauthenticated connection impersonating a worker; Go and C# refuse that same
connection. Narrowing Python to the browser path would complete the alignment,
but its e2e harness relies on the bypass to attach a worker, so that change
requires configuring `worker_bearer_token` in the harness first.

## Enforcement

Contracts that are only written down drift. These are executed:

- **`ci/hostile_probe.sh burst`** — 200 unauthenticated browser connects, every
  one must be refused. This is what caught the C# bypass.
- **`ci/hostile_probe.sh worker`** — the worker socket, added with this work.
  Three sequential attempts, not a flood: a session has exactly one worker, so
  concurrent registrations collide over that slot and surface as errors rather
  than as the auth answer. Verified against a C# server with its worker gate
  temporarily removed, where it fails as it should.
- **`BrowserWsAnonymousRefusalTests`** (C#) — refusal on a public session,
  indistinguishability of a missing session from a present one, and a negative
  control that an authenticated socket still connects.
- **`TestBrowserWSAnonymousRejected` / `TestBrowserWSInsufficientRole` /
  `TestWorkerWSTokenAuth`** (Go) — assert the pre-upgrade status codes.

A probe for these surfaces must assert on the **outcome**, not on whether
`connect()` returned. Any probe that stops at the handshake will report a
post-upgrade refusal as an auth bypass, and will report an accepted socket that
happens to stay silent as a timeout.

## Consequences

- A client can treat all three ports identically: a failed handshake is a
  refusal, `401` means authenticate, `403` means not permitted.
- Go's refusal no longer carries a reason string, since there is no socket on
  which to deliver a close reason. The status code carries the meaning instead,
  which is what the other two ports already did.
- Python carries a log filter for an upstream uvicorn defect. It is scoped to
  this server's own denials and deletable the moment uvicorn's completion check
  accounts for `initial_response`.
- The Python test-mode bypass remains broader than the other ports', recorded
  above rather than silently tolerated.
