<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# provide-uterm-ts

A high-coverage partial runtime port of the provide-uterm terminal session
platform. Its completed libraries are wire-compatible with the Python
reference and are held to 100% line, branch, and function coverage. The
integrated Node server is not yet a full backend. It runs on Node.js >= 22 and
is built with TypeScript 7.

It is distinct from the two browser packages:

| Package | Scope |
|---|---|
| `provide-uterm-frontend` | Browser widgets (lit + xterm.js), shipped to the SPA |
| `provide-uterm-app` | Browser SPA shell (React) |
| **`provide-uterm-ts`** | **Partial runtime port: completed libraries plus an incomplete Node server, client, and CLI** |

## Runtime maturity

Library completion and server integration are separate milestones. Foundation,
wire-format, terminal, policy, hub, connector, gateway, authentication, and
configuration libraries can be complete and fully tested without every one of
them being reachable through the running server.

The Node server currently binds these four shared HTTP capabilities:

| Capability | Route |
|---|---|
| `sessions.list` | `GET /api/sessions` |
| `sessions.get` | `GET /api/sessions/{session_id}` |
| `sessions.snapshot` | `GET /api/sessions/{session_id}/snapshot` |
| `sessions.set_mode` | `POST /api/sessions/{session_id}/mode` |

It also serves `/api/health`, `/healthz`, and `/readyz`, plus the REST hijack
lease operations (`acquire`, `heartbeat`, `snapshot`, `send`, `step`, and
`release`). Session create/update/delete and the remaining lifecycle/data
routes, the hijack events poll, REST rate-limit wiring, and browser/worker
WebSocket endpoints are still outstanding.

## Compatibility contract

The Python packages remain the reference implementation. Every module that
touches a wire format or an observable text transformation is proven against
CPython with a **differential corpus**: a `testdata/gen_*_golden.py` script
runs the Python code (via `uv run` at the repository root) over a
deterministic corpus of inputs and records the outputs. The corpus is
committed so CI re-verifies parity without a Python toolchain, and the
TypeScript tests must match it byte-for-byte.

Regenerate a corpus after a reference-implementation change:

```bash
uv run python packages/provide-uterm-ts/testdata/gen_colors_golden.py
```

| Module | Parity evidence |
|---|---|
| `colors` | 894-record CPython corpus (RGB quantisation, SGR rewriting, text + latin-1 byte paths) |
| `defaults` | Constant-for-constant assertion against `TerminalDefaults` |
| `pycompat` | CPython `round()` tie-breaking table |
| `server` | 17 probes recorded off the **running** reference FastAPI server on an ephemeral port, in `dev_token` mode, with the default configuration — status, body and headers, masked only where the live scenarios declare a field volatile |
| `server` (leases) | a 29-probe *sequence* recorded in order off the same running reference with its session's worker attached: the session snapshot, the input mode, and the whole hijack lease lifecycle with every way of being refused one. A sequence rather than a set because each answer depends on the ones before it, and a later probe quotes an earlier one's hijack id |
| `serverauth` | 45 token vectors driven through the reference's own JWT path (every way a token can be right and every way it can be wrong), the bearer-header grammar, and what `setup_dev_idp` mints |
| `server` (RBAC) | The whole `LocalAuthorizationProvider` decision table: what each role grants, how scopes narrow it, and who may read a session of each visibility |

## Development

```bash
npm run test:ts            # vitest
npm run test:ts:coverage   # vitest + 100% line/branch/function gate
npm run typecheck:ts       # tsc 7, strict
npm run build --workspace=packages/provide-uterm-ts  # emit dist/
npm run lint:ts            # biome
```

### Test-driven development

Every module in this port is written test-first:

1. **RED** — generate the golden corpus from CPython, write the test file
   against the module's intended public API, and add a stub module whose
   functions throw. Run the suite and confirm *every* new test fails on
   behaviour rather than on a missing import.
2. **GREEN** — implement until the suite passes.
3. **REFACTOR** — clean up with the suite staying green.

Expected values are never hand-guessed. Anything not covered by a generated
corpus is taken from a recorded `uv run python -c ...` invocation and the
source is noted in a comment beside the assertion.

### Live conformance driver

`src/conformance/` is this port's driver for the cross-language live harness
(`conformance/live/PROTOCOL.md`). Node strips the types, so it runs from
source:

```bash
node packages/provide-uterm-ts/bin/uterm-conformance.mjs \
  client --base-url URL --token TOKEN --scenario FILE
```

Both roles are runnable:

```bash
node packages/provide-uterm-ts/bin/uterm-conformance.mjs serve --auth dev_token
node packages/provide-uterm-ts/bin/uterm-conformance.mjs \
  client --base-url URL --token TOKEN --scenario FILE
```

The **client** role writes one line of JSON matching
`conformance/live/schema/result.schema.json` and evaluates nothing: every
expectation is the harness's to judge. It goes through the real
`HijackClient`, with a `fetch`-backed transport underneath it that records the
status code the library drops — so a 401, a 403 and a 404 stay three different
observations.

The **server** role stands the current partial `src/server/` integration up on
an ephemeral port (bind zero,
report what the operating system gave you — nothing in this repository may
name a port), announces its base URL and a token, and serves until stdin
closes or the process is signalled. The token is minted by the `dev_token`
stub identity provider and verified by the ordinary `jwt` path, so a forged
one is refused by exactly the code a production deployment runs.

The server accurately announces no named live-conformance capability yet:
the protocol's server capabilities describe complete hijack surfaces, while
this server still lacks WebSockets and part of the HTTP lifecycle. The
TypeScript client role can still exercise complete Python, Go, and C# servers.

Run the matrix from the repository root:

```bash
PYTHONPATH=conformance/live uv run python conformance/live/harness \
  --servers python typescript --clients python typescript
```

Like `src/react`, it is kept off the default entry: it reads files and writes to
standard output, and `provide-uterm-ts/conformance` reaches it by name.

### Conventions

- 100% line, branch and function coverage is enforced by
  `vitest.config.ts`. Barrels (`index.ts`) and `src/testing/` are excluded as
  pure re-exports and test scaffolding.
- ESM with explicit `.ts` import specifiers, rewritten to `.js` on build.
- `src/pycompat/` holds CPython semantic shims (rounding, and later string
  and encoding behaviour) so the same reference semantics are not
  re-derived per module.
- Biome disables `noControlCharactersInRegex` for this package: ANSI escape
  parsing needs literal `\x1b` in patterns.
- New files carry the SPDX header block.
