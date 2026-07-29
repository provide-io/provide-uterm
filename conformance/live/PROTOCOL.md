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

### The announcement means ready, not bound

A driver must not announce until the server can actually serve the scenarios —
which includes any session the configuration marks `auto_start` having come
up. Binding the socket is not enough.

This is not theoretical. The reference server reports its configured session
as `stopped` for roughly two tenths of a second after its socket is listening.
A Python client takes about a second to start, so it never sees that window; a
compiled Go or C# client starts in milliseconds and lands inside it, finds a
session with no worker attached, and cannot take a lease on it. The race fired
for some client languages and not others — which is the worst way for a
harness to be wrong, because it reads as a difference between the languages
rather than as a difference in when they arrived.

Waiting should be bounded. A session that never settles is worth announcing
anyway and letting the scenario report what it finds: a harness that hangs
says less than one that fails.

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
| `session_events` | a session's recent events | `status`, `ok`, `body` |
| `set_input_mode` | put a session in `open` or `hijack` mode | `status`, `ok`, `body` |
| `hijack_acquire` | take the lease on a worker | `status`, `ok`, `body` |
| `hijack_heartbeat` | extend the lease | `status`, `ok`, `body` |
| `hijack_send` | send `keys` to a hijacked worker | `status`, `ok`, `body` |
| `hijack_step` | single-step a hijacked worker | `status`, `ok`, `body` |
| `hijack_snapshot` | the screen, through the lease | `status`, `ok`, `body` |
| `hijack_release` | give the lease back | `status`, `ok`, `body` |
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

## A step that needs an earlier step's answer

`hijack_send` needs the `hijack_id` that `hijack_acquire` returned. Nothing in
the first wave of actions had this shape — every step was independent.

A string field in a step may therefore hold a **reference**:

```json
{ "id": "send", "action": "hijack_send", "hijack_id": "${acquire.body.hijack_id}", "keys": "echo hi\n" }
```

`${<step id>.<dotted path>}` is resolved by the driver, against the fields it
has already recorded, at the moment it builds the request. The grammar is
deliberately the smallest thing that works: one step id, one dotted path, no
expressions, no defaults, no nesting, and the whole field must be the
reference — `"a${x.y}b"` is not a reference and is sent as written.

The harness cannot do this resolution, which is worth stating because it is
the one place the "drivers observe, the harness judges" rule does not reach:
the driver performs the request, so the driver must hold the value before
anyone else could have seen it.

Four implementations of one small thing is four chances to disagree, so the
risk is paid down directly: the resolver gets a scenario of its own, whose
steps do nothing but refer to each other's answers. A driver whose resolver is
wrong then fails a cell that has nothing to do with hijacking, and the failure
lands on the resolver rather than on whatever feature happened to use it.

A reference that names a step that has not run, or a path that is not there,
is a **run error** — not a step observation. It is a malformed scenario, and
recording it as a field would let the harness compare it as though the server
had done something.

The same rule holds for a step **missing an argument its action needs** — a
`hijack_send` with no `hijack_id`, an `http_get` with no `path`. It is a run
error, for the same reason.

Both are belt and braces: the harness refuses either at load, so a committed
scenario cannot reach a driver in that state. The rule is written down anyway
because four drivers reached four different answers when it was not, and a
disagreement about what a *malformed* scenario does is still a cell that fails
for a reason having nothing to do with any server.

Two smaller readings, settled the same way — by what the reference driver
does, since one of them has to be first:

* a reference may appear in **any argument field**, but never in `id` or
  `action`. A step that renamed itself could not be matched to its own
  expectations;
* a `body` written as a reference is substituted as **JSON**, not as text, so
  a step can post back an object it was handed.

## A step done more than once

Some behaviour is only observable by exhausting something. A rate limiter is
the case that forced this: it is invisible until the budget runs out, and the
only way to see it is to spend the budget.

A step may therefore carry `repeat`:

```json
{ "id": "flood", "action": "hijack_acquire", "worker_id": "provide-shell", "repeat": 31 }
```

The driver performs the step that many times and records **each repetition as
its own observation**, under `<step id>.<0-based index>` — `flood.0`,
`flood.1`, and so on. The bare `flood` records nothing.

Every repetition is recorded, never just the last. A scenario repeats a step
precisely when it expects the answers to *stop being the same*, so which
repetition changed is the measurement — a driver that recorded only the final
answer would turn "the thirty-first request was refused" into "a request was
refused", and those are different claims about a budget.

Expectations name the repetition they mean:

```json
{ "step": "flood.30", "path": "status", "equals": 429 }
```

Three rules, all refused at load rather than left to four drivers:

* **there is no `repeat: 1`.** A step that runs once keeps its bare id. Two
  ways to write the same thing, where one of them renumbers every observation,
  is a difference nobody would remember when reading a scenario;
* **an expectation may not name the bare id** of a repeated step. It records
  nothing, so the expectation would be about a step nobody runs — which passes
  in every cell at once, the one failure mode this harness exists to prevent;
* **a `${...}` reference may not name a repeated step.** It has as many answers
  as repetitions and the grammar cannot say which is meant: the step id admits
  no dot, so `${flood.2.body.x}` reads as step `flood`, path `2.body.x`. If a
  scenario needs the value, the step producing it should not be the repeated
  one.

`repeat` is capped at 200. It is the only field that can turn one scenario
into a load test, and a scenario runs in every cell of the matrix.

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
