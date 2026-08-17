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
| Python | 403 pre-upgrade | 403 pre-upgrade |
| Go | 401 pre-upgrade | 403 pre-upgrade |
| C# | 401 pre-upgrade | 403 pre-upgrade |

The pre-upgrade rule now holds everywhere. One divergence remains, deliberately.

### Why Python answers 403 where the others answer 401

Not a design choice — an upstream defect, and every way around it costs more than
it buys.

Starlette denies a pre-accept WebSocket refusal with a hardcoded **403**.
Emitting 401 requires the ASGI websocket-denial-response extension
(`WebSocket.send_denial_response`). That was implemented and measured, not
estimated: the client receives `401` with `WWW-Authenticate: Bearer` on both
sockets, all four hostile-client probes pass, and the availability lane's ten
authenticated sessions complete in 0.06s against 0.03–0.05s on the default
implementation.

It was reverted because of how uvicorn handles the denial. The sansio
implementation sets `initial_response` for a denial but never sets
`handshake_complete`, and its `run_asgi` checks only the latter
(`websockets_sansio_impl.py:416`), so **every refusal logs an ERROR and attempts
a 500**. Two adjacent branches in the same file already get this right
(`:425`, `:438` both test `initial_response`), as does `wsproto_impl.py:349` via
`response_started`. Two hundred unauthenticated connects produce two hundred
error lines and two hundred attempted 500s — log amplification on precisely the
path a flood targets, which is a worse property to ship than an imperfect status
code.

Which implementation is in use decides it, and the clean ones are the ones going
away:

| `--ws` | denial behaviour |
|---|---|
| `auto` (the default) | ERROR + attempted 500 |
| `websockets-sansio` | ERROR + attempted 500 |
| `wsproto` | clean |
| `websockets` (legacy) | clean, but **deprecated** — uvicorn warns it will point at the sansio implementation |

So the working paths are a deprecated implementation and a non-default one. A
standalone reproducer (pure Starlette + uvicorn, no application framework) and a
proposed one-line patch are drafted at
`../uvicorn-websocket-denial-response-issue.md`, outside this repo.

What moving Python to 401 would actually involve, in full:

1. ~40 lines in `app/factory_impl.py` — the denial helper, a sentinel exception
   so the endpoint stops without Starlette re-closing a finished connection, and
   a no-op handler for it. Written and working.
2. Two tests, because this package enforces 100% branch coverage and the helper
   has a fallback branch.
3. `ws="wsproto"` at the `uvicorn.run` call site.
4. Declaring `wsproto` as a dependency. It is currently **transitive only** —
   zero mentions in any `pyproject.toml` — and pinning an undeclared package is
   how a deployment that installs plain uvicorn dies at startup.
5. Accepting `wsproto`, a pure-Python implementation, as the WebSocket transport
   for every session this server carries. The measurement above covers ten
   concurrent sessions; it says nothing about production load.

Item 3 also does not hold where it matters most. The pin would live in `cli.py`,
but the app is launched other ways — `uvicorn.Server(uvicorn.Config(app, ...))`
in the TypeScript golden generators and in this package's own test conftest.
Those keep the default implementation, so the status code would depend on how
the server was started. One documented divergence is better than a conditional
one.

The position is therefore conditional on the upstream fix, not on preference.
When `websockets_sansio_impl.py:416` accounts for `initial_response`, Python
moves to 401 with no dependency swap and no launch-path caveat, and the table
above becomes uniform.

Consumers are unaffected by the difference: `runtime_helpers.py` classifies
`401`, `403`, and `404` alike as permanent, so a worker that is refused does not
retry against any port.

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
- Python's 401 remains blocked on an upstream uvicorn fix, and the Python
  test-mode bypass remains broader than the other ports'. Both are recorded
  above rather than silently tolerated.
