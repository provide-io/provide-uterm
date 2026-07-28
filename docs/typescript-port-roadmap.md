<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# TypeScript port roadmap

Tracks `packages/provide-uterm-ts`, the fourth full port of the platform,
towards feature parity with the Python reference and the Go and C# ports.

Last updated: 2026-07-28.

## Contract

Every module is ported test-first against a committed differential corpus
generated from CPython. See `packages/provide-uterm-ts/README.md` for the
workflow and `.ci/check_ts_goldens.sh` for the drift gate that keeps the
corpora honest.

Definition of done for a module:

1. A `testdata/gen_<name>_golden.py` generator recording CPython behaviour.
2. Tests written against a throwing stub and observed failing on behaviour.
3. Implementation, then 100% line/branch/function coverage.
4. `tsc` 7 strict clean, biome clean, corpus drift check clean.
5. Any dialect divergence recorded as an executable assertion, not a comment.

## Runtime dependencies

Approved 2026-07-28. Each unblocks a subsystem that cannot be written against
the standard library alone.

| Package | Subsystem | Note |
|---|---|---|
| `ws` | WebSocket server | Node has a WebSocket *client* but no server |
| `ssh2` | SSH transport and gateway | |
| `node-pty` | PTY connector | Native. See the note below |
| `@modelcontextprotocol/sdk` | MCP tools | The protocol's own SDK |
| `smol-toml` | TOML server config | Spec-complete and dependency-free |

These are Node-facing. Nothing a Cloudflare Worker imports may reach them —
the same bar `api-routes` already meets for the browser SPA.

**No HTTP router.** An earlier revision of this file recommended `hono`. That
recommendation is withdrawn: this port now has `api-routes`, which matches a
method and path to an operation, extracts the parameters and reports the
allowed methods for a 405. That is what the router would have been used for,
and it is the table both Python backends already dispatch from. Adding a
second router would mean two sources of truth for the same routes, which is
exactly what the shared contract exists to prevent. Node's `http` and a
Worker's `fetch` each hand over a method and a URL; the registry does the
rest.

**VNC** stays hand-rolled — there is no server-side RFB package worth taking,
and the Go and C# ports hand-roll it too.

**Tunnel token hashing is resolved — no dependency needed.** The Durable
Object authenticates share and control cookies against stored
`blake2b(digest_size=32)` digests. Node's `crypto` exposes BLAKE2b only as
`blake2b512`, and truncating that is *not* the same value — the digest length
is part of BLAKE2b's parameter block, so the two disagree from the first byte.
`pycompat/blake2b` implements RFC 7693 with a configurable output length and
is pinned against CPython for eighteen inputs, including either side of the
128-byte block boundary. `serverauth/token-hash` is the store on top of it.

**`node-pty` needs one more step.** Its prebuilt `spawn-helper` ships
non-executable and its post-install script, which is what marks it, is held
by this repository's `npm approve-scripts` policy. Until that is resolved
`spawn` fails with `posix_spawnp failed`. Approving a third-party install
script is a decision for the repository owner, so it is recorded here rather
than taken.

## Status

Ordered bottom-up by dependency. The Python column names the reference
module; the Go column names the sibling package for cross-checking.

### Foundation

| Module | Python | Go | Status |
|---|---|---|---|
| `pycompat` | — (CPython semantics) | — | **done** — json, re, rounding, str, statistics, difflib, int, ipaddress, blake2b |
| `defaults` | `defaults` | `defaults` | **done** |
| `colors` | `colors` | `colors` | **done** |
| `sanitizer` | `sanitizer` | `sanitizer` | **done** |
| `redaction` | `redaction` | `redaction` | **done** |
| `filters` | `filters` | `filters` | **done** |
| `control-channel` | `control_channel`, `ws_bytes` | `controlchannel` | **done** |
| `line-editor` | `line_editor` | `lineeditor` | **done** |
| `channels` | `channels` | `channels` | **done** |
| `file-io` | `file_io` | `fileio` | **done** |
| `ansi` | `ansi`, `_ansi_dialects` | `ansi` | **done** |
| `screen` | `screen` | `screen` | **done** |
| `policy` | `bridge/policy` | `policy` | **done** — held to `spec/behavior_vectors.json`, the same 48 cases as Python, Go and C# |
| `replay` | `replay` | `replay` | **done** — the raw-stream rebuild and the log viewer, with its frame selection and delay schedule |

### Wire formats

| Module | Python | Go | Status |
|---|---|---|---|
| `ctrlmsg` | `control_channel_builders`, `control_channel_patterns` | `ctrlmsg` | **done** |
| `frames` | `bridge/schemas`, server `bridge/frames` | `frames` | **done** |
| `recording` | `recording` | `recording` | **done** |
| `session-logger` | `session_logger` | `sessionlogger` | **done** |

`ctrlmsg` carries the HMAC identity signature, which is taken over
CPython-canonical JSON, so the float divergence recorded in the
control-channel corpus is not acceptable there. `pycompat/json.ts` supplies
the exact serialisation and the 24 signed identity frames in the `ctrlmsg`
corpus match CPython byte for byte.

### Terminal

| Module | Python | Go | Status |
|---|---|---|---|
| `vt` | pyte (dependency) | `vt` | **done** |
| `emulator` | `emulator` | `emulator` | **done** |
| `render` | `render` | `render` | **partial** — SGR row rendering; image/palette/segments outstanding |
| `detection` | `detection` | `detection` | **done** — screen buffer, input-type heuristic, prompt detector (compilation, two-pass matching, exclusions, the cursor heuristic and its fallback, fingerprinting), rule schema, key-value extractor, flow controller, rule loader, engine and screen saver |
| `deckmux` | `deckmux` | `deckmux` | **complete** — protocol, presence store, control transfer, derived names/colours, edge geometry, SSH identity bridge and the presence service. `_hub_mixin.py` is deliberately not ported: it owns no state and exists only to keep Python call sites working (the mixin shape, monkey-patched `deckmux_*` test hooks, and the legacy `hub._presence_stores` attribute names). Go and C# both skip it too — `packages/provide-uterm-go/deckmux/` is exactly the same seven modules — so a hub composes `DeckMuxPresence` directly |
| `annotation` | `provide-uterm-annotation` | `annotation` | **done** — the 20 built-in rules, the detector and the streaming boundary carry |

### Session and transport

| Module | Python | Go | Status |
|---|---|---|---|
| `session` | `io`, `expect`, protocols | `session` | **done** — guarded sends, prompt waiting, input dispatch, bounded capture, the transport session and the telnet/websocket adapters |
| `termsession` | `transport_session`, `telnet_session`, `ws_session` | `termsession` | todo |
| `transports` | client `transports/*` | `transports` | **partial** — the transport interface, telnet RFC 854 framing and negotiation, the reconnect budget/backoff, the WebSocket client, the chaos wrapper, the full RFC 854 telnet client and the SSH session stream adapters; the SSH server itself outstanding |
| `egress` | server `egress`, `_net` | (inside `connectors`) | **done** — metadata always blocked, embedded-IPv4 wrappers decoded, resolution failures fail closed |
| `connectors` | server `connectors/*` | `connectors` | **partial** — the registry, the connector interface and the reference shell connector; the ssh/telnet/websocket connectors outstanding |
| `pty` | platform PTY connector | `pty` | todo |
| `embed` | `embed` | `embed` | todo |

### Control plane and hub

| Module | Python | Go | Status |
|---|---|---|---|
| `auth` | `auth` | `auth` | **done** — OpenSSH fingerprints, the authorized_keys grammar, and both reference resolvers |
| `serverauth` | server `auth*`, `webhook*`, `api_keys`, `dev_idp` | `serverauth` | **partial** — webhook signing, the RBAC allow-list, the API-key store and the tunnel token hash; the auth modes and dev IDP outstanding |
| `serverconfig` | server `config*`, `profiles` | `serverconfig` | **partial** — the outbound-URL guard, mount-path normalisation, every cross-field validator, TOML loading with its structural pass and relative-path resolution, the security-posture report and the security response headers; the Pydantic schema itself and profiles outstanding |
| `controlplane` | `control/plane` (+ memory/sqlite/bootstrap) | `controlplane` | **partial** — the record types, the in-memory backend with its optimistic concurrency, the reaper, the audit head and the bootstrap; the SQLite backend outstanding |
| `hub` | server `bridge/hub` (nine services) | `hub` | **done** — all nine services, plus the state model, frame encoders, prompt guards and the regex-safety validator |
| `bridge` | `bridge` worker side | `bridge` | **done** — authorization matrix, hijack coordinator, protocol contract, hijackable primitives, the worker link and the hello protocol-range reader |
| `fanout` | server `bridge/fanout` | `fanout` | **done** — divergence detection, group records, the in-memory store, the output collector, the controller and the REST routes |
| `graphical` | graphical target registry | `graphical` | todo |
| `gui` | `vnc`, GUI ops | `gui` | todo |

### Server and edge

| Module | Python | Go | Status |
|---|---|---|---|
| `api-routes` | `api_routes` (the shared HTTP contract) | — | **done** — the whole table, template validation, shadowing detection, matching and the capability check |
| `server` | server `routes/*`, `app/*`, `runtime` | `server` | **partial** — binding the shared contract to a backend's handlers, with its capability and role-guard validation and a framework-neutral dispatcher; the handlers, app wiring and runtime outstanding |
| `client` | client `hijack`, `control_ws` | `client` | todo |
| `gateway` | server `gateway` | `gateway` | **partial** — the SSH server's security policy (loopback bind test, host-key permissions, per-IP limits, permissive-auth refusal) the host-key store (including an OpenSSH ed25519 encoder written from the format, because Node emits only PKCS#8), the SSH server itself, and the WebSocket server — both stood up on an ephemeral port and connected to for real |
| `tunnel` | `tunnel`, `tunnel_invites`, `pam_tunnel` | `tunnel` | todo |
| `tunnelclient` | share/inspect client | `tunnelclient` | todo |
| `vnc` | `vnc` | `vnc` | todo |
| `shell` | `shell` | `shell` | todo |
| `manager` | External Management Tier | `manager` | todo |
| `mcp` | client `ai`/MCP (28 tools) | `mcp` | todo |
| `cli` | server CLI (`uterm`) | `cli` | todo |
| `cloudflare` | `provide-uterm-cloudflare` (53 files, ~8.3k lines) | — | **partial** — the Worker configuration with its startup refusals, the KV session registry, and the whole Durable Object state store (schema, session metadata, invite state, lease state, snapshots, event log, recording view, webhooks and resume tokens); the session runtime's flow controller, its WebSocket attachment reading, webhook secret encryption, the polling server-sent-events endpoint, the whole JWT auth path (token splitting, JWKS key choice and its caching endpoint, standard-claim validation, RS256 signature verification, role derivation, principal derivation and token extraction), the session runtime's own auth (share-cookie roles with their expiry and address binding, and the owner elevation), and its socket registry; the Worker entry, the rest of the session runtime, transport, auth and routes outstanding |

#### Cloudflare Durable Objects

Python-only: neither the Go nor the C# port has any Cloudflare surface, so
this is a place where TypeScript lags the *reference* rather than its
siblings. The Python package covers the Worker entry point, the Durable
Object adapter (`do/ushell.py` with its persistence, SSE and webhook
helpers), the CF transport, JWT auth, route definitions and UI assets.

**This is additive.** The Python Worker stays exactly as it is — same
deployment, same vendored `python_modules`, same `.ci/vendor_cf_worker.sh` and
its CI guard. The TypeScript implementation is a second, independent one at
parity with it, not a migration path away from it. Nothing about the existing
Worker changes.

It needs no new runtime dependency: the Workers types are dev-only
(`@cloudflare/workers-types`) and `wrangler` is already in the repo.

Sized as a large unit — comparable to the hub — and worth taking as one
subsystem rather than in pieces, because the Worker entry, the DO and the
transport only make sense together. Parity is measured the same way as
everywhere else in this port: a differential corpus recorded from the Python
implementation, so the two answer identically on the same input.

### Browser

The existing `provide-uterm-frontend` (lit widgets) and `provide-uterm-app`
(React SPA) stay where they are. Two tasks belong to this port:

| Task | Status |
|---|---|
| Move both browser workspaces onto TypeScript 7 | **done** |
| React SPA covers every bootstrapped page kind | **done** |
| SPA consumes `provide-uterm-ts` rather than restating it | **started** — its API paths and methods now come from the shared route table |
| Port the two standalone lit entry points to React | not planned — see below |

The SPA depends on `provide-uterm-ts` as a workspace package and builds every
request from `API_ROUTES`, so a route that moves on the Python side reaches
the browser as a failing test rather than a 404. `api-routes` is deliberately
free of any `pycompat` import for this reason — that package reaches for
Node's `Buffer`, which has no place in a browser bundle. Anything else the
SPA takes from the runtime port has to clear the same bar.

The SPA already handles all six `page_kind` values the server bootstraps:
`connect`, `dashboard`, `inspect`, `operator`, `replay` and `session`. That
was verified by diffing the `case` arms in `App.tsx` against every
`page_kind` literal in the server, not by reading the component tree — an
earlier revision of this file claimed the SPA was incomplete on the strength
of a truncated directory listing, which was simply wrong.

Two lit pages remain outside the SPA on purpose: `vnc.html` and
`panels.html` are separate static entry points with their own bundles, not
routes the bootstrap dispatches to. Folding them into the SPA would mean
loading noVNC and the panel machinery into every page's bundle to serve two
pages that are opened directly. They stay as they are unless the bootstrap
starts routing to them.

## Known cross-port misalignments

### A malformed prompt rule behaves differently in all three ports

`{"regex": 123}` — a rules file with a non-string regex:

- **Python** raises `TypeError` out of `re.compile`, which `compile_patterns`
  does not catch, so the detector fails to construct *even in lenient mode* —
  the one mode whose purpose is to survive a broken rule.
- **Go** coerces it through `asString` to `""`, which compiles to a pattern
  matching every screen. One typo becomes a rule that always fires.
- **TypeScript** records it as a compile failure and skips the rule, which is
  the only reading that does what lenient mode says it does.

Relatedly, a non-string `input_type` is refused by Python's Pydantic model
with a `ValidationError` mid-detection; the TypeScript port falls back to the
documented default rather than ending a session over a cosmetic field. And a
`negative_regex` of `null` becomes the string `"None"` in Python, `""` in Go
(no exclusion), `"null"` in TypeScript.

All are recorded in `detector_golden.json`. Fixing them means agreeing on one
behaviour — most likely "record a compile failure and skip" — across all four
ports at once.

### Identity-frame version accepts a boolean in Python only

`parse_identity_frame` tests `version in frozenset({1})`. Python's `True == 1`,
so a frame carrying `"version": true` is read as version 1 and accepted. Go's
`identityVersion` (`deckmux/identity.go`) accepts only the int and float forms
and refuses a bool, so the two ports already disagree; the TypeScript port
follows Go. Recorded in `deckmux_identity_golden.json` as
`python_boolean_version` so the divergence stays visible. Fixing it means
tightening the Python check, which is a behaviour change in the reference and
should land across all four ports together.

Found while porting. Neither is a missing feature; both are worth settling.

**Webhook signature ambiguity.** The signed material is
`timestamp + "." + body`, so a body beginning with digits and a dot can be
re-read as part of the timestamp: a signature over `0.body` at `17000000` is
byte-identical to one over `body` at `17000000.0`, and since both timestamps
name the same instant the second *verifies*. Confirmed against the Python
reference, and shared by Go and C#. Not reachable for the JSON bodies the
governance webhook actually sends (they begin with `{`), but the scheme is
ambiguous in principle; the fix — length-prefixing or hex-encoding the
timestamp — has to land in all four ports together. The TypeScript port
matches the current behaviour and pins it with a test that says why.

**SSH placement.** Go and C# put the SSH client in `transports/` and the SSH
server in `gateway/`. Python inverts both: the asyncssh *server* is in
`provide-uterm-client/.../transports/ssh.py` and the asyncssh *client* is in
`provide-uterm-server/.../connectors/ssh.py`. Every port has both sides, so
this is placement only — but it puts a server in a client package and a client
in a server package, and it makes cross-port comparison read as a gap (the Go
`transports/ssh.go` header comment records it as one, scoped to that package).
This port follows the Go/C# layout. Re-homing the Python side is a separate
change.

**Chaos jitter.** Deliberately language-specific: each port draws the delay
from its own seeded generator, as the Go port already did. What is aligned,
and corpus-pinned, is the fault *schedule* — which read drops, which returns
empty, and the injected message.

## Cross-language obligations

Landing the port is not only new files. These existing artefacts have to
learn about a fourth implementation:

| Artefact | Change needed | Status |
|---|---|---|
| `spec/behavior_vectors.json` | `policy_cases` consumed directly by the TS suite | **done** |
| `spec/behavior.json` | add a `typescript` entry to `hello_defaults` | todo — waits on the TS server, which has to *have* the capabilities before it can claim them |
| `docs/security-language-parity.md` | add a TypeScript column | todo |
| `docs/protocol-matrix.md` | add a TypeScript column | todo |
| `conformance/live` | run the live scenarios against the TS server | todo |
| `scripts/check_max_loc.py` | 777-line cap covers `.ts` | **done** |
| `ci/quality_checks.sh` | golden-corpus drift gate | **done** |
| `.github/workflows/ci.yml` | typecheck, lint and coverage in `npm-quality` | **done** |

## Telemetry: fixed upstream, waiting on a release

The port must log through `provide.telemetry`, never through OpenTelemetry
directly — the same rule the Python reference and the Go port follow. The
TypeScript sibling is the right dependency: `@provide-io/telemetry`, which
exports `getLogger` / `Logger` (the analogue of Go's `ptel.GetLogger`) and
keeps OpenTelemetry as an optional peer dependency with a browser no-op.

`@provide-io/telemetry@0.5.2` — still what npm serves — cannot be imported
from Node at all. It declares `"type": "module"` but its `dist/` contains 115
extensionless relative imports (`from './config'`), which Node's ESM resolver
rejects:

```
$ node --input-type=module -e "import('@provide-io/telemetry')"
ERR_MODULE_NOT_FOUND: Cannot find module '.../dist/config'
    imported from .../dist/index.js
```

Root cause was in `provide-telemetry/typescript/tsconfig.json`: it built with
`"moduleResolution": "bundler"` and emitted with plain `tsc`. Bundler
resolution allows extensionless specifiers in source, `tsc` does not rewrite
them on emit, and the result loads only under a bundler.

**Fixed upstream** in `provide-telemetry` (`292d306c`, not yet published):
`nodenext` resolution with explicit `.js` specifiers across source, tests and
scripts. Nothing already in that repo's pipeline could have caught this —
lint, typecheck and its 1666-test vitest suite all resolve like a bundler and
were green the whole time it was broken — so the fix also adds
`ci/verify-npm-consumer-package.sh`, which packs the tarball and imports every
entry point from a real Node process. It was verified to exit 1 against a
`dist/` with the extensions stripped back off.

**Remaining step: publish.** This port picks the dependency up when a release
carrying that fix reaches npm.

**What is unblocked meanwhile.** The layering the Go port uses — library
packages take an injectable logger, only transports and above call
`ptel.GetLogger` — does not need the concrete implementation. `src/telemetry/`
declares the `Logger` interface structurally, matching the one
`@provide-io/telemetry` exports, plus a `noopLogger` default. Library modules
depend on that and stay decoupled and testable today.

`src/telemetry/get-logger.ts` is a **deliberate stand-in** for the concrete
`getLogger`, so the layers above the libraries are not held up. It emits the
same structured record shape (name, level, field object, optional message)
behind the same `Logger` interface, which makes it a re-export away from the
real thing: when the release lands, that module collapses to an export line
and no caller changes.

What it deliberately does **not** do, because faking it would be worse than
its absence: trace and span correlation, sampling, PII redaction, and OTLP
export. Those are precisely why the real package is the target rather than
this being treated as sufficient. Deleting it is the definition of done for
this section.

When the release lands, `src/telemetry/` re-exports the real `getLogger` and
swaps its interface for the imported one — a type-level no-op for every
caller — and becomes the only place the package name appears, with a CI check
forbidding direct `@opentelemetry/*` imports.

## Recorded divergences

Deliberate, tested differences from the CPython reference. Each is asserted
in a test so it cannot drift silently.

| Area | Divergence | Rationale |
|---|---|---|
| `redaction` | `\d` and `\w` are ASCII-only, not Unicode-aware | Host engine semantics; Go's RE2 has the same boundary |
| `redaction` | A leading `(?i)`/`(?m)`/`(?s)` is translated into RegExp flags | ECMAScript has no inline-flag syntax, so the alternative is a hard compile error |
| `control-channel` | JSON renders `0.0` as `0` | Go and C# do the same; the canonical-JSON signature path will not |
| `colors` | `rewriteParams` passes a non-digit SGR component through where CPython raises | Unreachable from the SGR scanner; passing through is the safer of the two |
| `emulator` | `resize` clips at the top deterministically, where the reference varies by whether the screen was read first | The reference buffer materialises rows on read, so a shrink behaves differently; deterministic clipping is what it documents |
| `vt` | An unhandled C0 control is drawn, where pyte stalls its parser and swallows the rest of the stream | Matches the Go port; a stray byte freezing the display permanently is a bug, not a contract |
| `pycompat` | An integral number renders as a Python `int`; insertion order is not preserved for integer-like object keys | JavaScript has one number type and reorders such keys — neither affects the canonical signing path |
| `ansi` | Token digit classes are ASCII-only, so a Unicode-digit token is left verbatim | Same `\d` boundary as `redaction`; the patterns spell `[0-9]` to make the intent explicit |
| `serverconfig` | `security.mode` is compared verbatim for headers and normalised for the posture report | The reference's own inconsistency, carried over: a config writing `STRICT` reports as strict and serves the relaxed headers |
| `cloudflare/sse` | An event field holding a whole-valued float renders as `1700000000`, not `1700000000.0` | The `pycompat` number divergence reaching a wire format: both ends read the field as JSON, so the value survives — only the bytes differ |
| `cloudflare/sse` | An `after_seq` beyond 2^53−1 is rounded to the nearest double | Both then ask for events after a sequence larger than any that exists, so the answer is the same; the number asked for is not |
| `cloudflare/jwt` | A `null` inside a roles claim stringifies to `"null"`, not `"None"` | `str(None)` against `String(null)`; inert either way, since neither is a role this system knows and the principal resolves identically |
