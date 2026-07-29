<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# The live driver protocol

Every language in this repository ships one **driver**: a small executable that
can act as a server or as a client, and that speaks this protocol to the
harness. The harness starts one server driver and runs every client driver
against it, once per language pair, and asserts that what came back is the
same in every cell.

The protocol exists so the harness never has to know how a language starts a
server, where its binary lives, or what its client library is called.

## Roles

A driver takes one subcommand.

### `serve`

```
<driver> serve [--auth MODE] [--scenario FILE]
```

Bind a server on an **ephemeral port**, then write one line of JSON to stdout
and keep serving until stdin closes or the process is signalled:

```json
{"role": "server", "language": "python", "base_url": "http://127.0.0.1:54321",
 "token": "<a token the client should present>", "capabilities": ["hijack.rest"]}
```

The port is never chosen by the harness or written in a scenario. A driver
binds port 0 and reports what the operating system gave it. Nothing in this
directory may name a port.

Closing stdin is the ordinary shutdown. A driver that has not exited within
the harness's grace period is killed.

### `client`

```
<driver> client --base-url URL --token TOKEN --scenario FILE
```

Run the scenario's steps in order against `URL` and write one line of JSON to
stdout — a **result** (`schema/result.schema.json`):

```json
{"role": "client", "language": "python", "scenario_id": "010_health",
 "status": "completed", "capabilities": [], "steps": [
   {"id": "health", "fields": {"status": 200, "ok": true, "body": {"status": "ok"}}}
 ]}
```

## Drivers observe. The harness judges.

A driver never decides whether a scenario passed. It performs each step and
records what it saw. Every expectation in every scenario is evaluated by the
harness, in one implementation, so four languages cannot disagree about what
an expectation *means* — only about what the server did.

This is the whole reason the results are comparable. A driver that evaluated
its own expectations would be reporting its reading of the contract; a driver
that reports observations is reporting the contract.

`status` is therefore about the *run*, not the verdict:

| `status` | Meaning |
|---|---|
| `completed` | Every step ran. Look at `steps` for what happened. |
| `unsupported` | The scenario needs a capability this language does not have. |
| `error` | The driver itself failed. `error` says how. |

A step that got an HTTP 500 still `completed` — the 500 is the observation.

## Steps

A step is `{"id": ..., "action": ..., ...}`. Every driver implements the same
action vocabulary; an action a driver does not know is an `error`, never a
silent skip.

| Action | What the driver does | Fields recorded |
|---|---|---|
| `health` | the client library's health call | `status`, `ok`, `body` |
| `list_sessions` | the client library's session list | `status`, `ok`, `body` |
| `get_session` | one session by id | `status`, `ok`, `body` |
| `session_snapshot` | a session's snapshot | `status`, `ok`, `body` |
| `http_get` | a raw GET of `path` | `status`, `ok`, `body` |
| `http_post` | a raw POST of `path` with `body` | `status`, `ok`, `body` |

The client-library actions exist so that what is under test is the library a
consumer would actually use, not a hand-rolled request that happens to agree
with it. `http_get`/`http_post` cover the surfaces no client method reaches.

### Recording the status a client library hides

Every port's `HijackClient` answers `(ok, body)` and drops the status code. So
a 401, a 403 and a 404 all arrive as the same `ok: false` — three different
refusals that a matrix could not tell apart, which is precisely the drift this
harness is for.

A driver therefore **observes the status underneath the library**, by
injecting a recording transport (Python `httpx.AsyncBaseTransport`, TypeScript
the `HijackClient` transport option, Go an `http.RoundTripper`, C# an
`HttpMessageHandler`). The library still performs the call and still shapes
`ok` and `body`; the transport only writes down what came back.

If a language genuinely cannot inject one, it records `status: null` and
reports the capability `status.observed` as absent — so the gap is in the
matrix rather than hidden in it.

So for a client-library action: `status` and `body` are what the transport
saw, and `ok` is what the library concluded. That split is deliberate. `ok` is
the library's own reading and worth comparing across languages; the body is
the server's answer and would otherwise be buried under each library's way of
wrapping a payload it could not parse.

Each step may carry `auth`:

| `auth` | Header sent |
|---|---|
| `token` (default) | the token the server driver reported |
| `none` | no `Authorization` header at all |
| `bad` | `Bearer <a token no server issued>` |

`body` is the parsed JSON body, or — when the body was not JSON — the string
`"<non-json>"`. A body nobody can parse is the same observation in every
language; the bytes are not.

## Capabilities

A scenario may require capabilities (`"requires": ["hijack.rest"]`). A driver
reports what it has in its `serve` line and in its result. When a required
capability is missing the driver reports `unsupported` and the harness records
the cell as unsupported rather than failing it — and prints it, because a
silently skipped cell is how a matrix comes to mean nothing.

## Registration

`harness/drivers.py` holds the table: language → how to start its driver. A
language whose driver is not built yet is reported as such by
`--list-drivers`, so an incomplete matrix is visible rather than implied.
