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

## Status

Ordered bottom-up by dependency. The Python column names the reference
module; the Go column names the sibling package for cross-checking.

### Foundation

| Module | Python | Go | Status |
|---|---|---|---|
| `pycompat` | — (CPython semantics) | — | **done** |
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
| `policy` | server policy | `policy` | todo |
| `replay` | `replay` | `replay` | todo |

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
| `detection` | `detection` | `detection` | **partial** — screen buffer and input-type heuristic; rule engine, extractor and flow outstanding |
| `deckmux` | `deckmux` | `deckmux` | todo |
| `annotation` | `provide-uterm-annotation` | `annotation` | todo |

### Session and transport

| Module | Python | Go | Status |
|---|---|---|---|
| `session` | `io`, `expect`, protocols | `session` | todo |
| `termsession` | `transport_session`, `telnet_session`, `ws_session` | `termsession` | todo |
| `transports` | client `transports/*` | `transports` | todo |
| `connectors` | server `connectors/*` | `connectors` | todo |
| `pty` | platform PTY connector | `pty` | todo |
| `embed` | `embed` | `embed` | todo |

### Control plane and hub

| Module | Python | Go | Status |
|---|---|---|---|
| `auth` | `auth` | `auth` | todo |
| `serverauth` | server `auth*`, `webhook*`, `api_keys`, `dev_idp` | `serverauth` | todo |
| `serverconfig` | server `config*`, `profiles` | `serverconfig` | todo |
| `controlplane` | `control/plane` (+ memory/sqlite/bootstrap) | `controlplane` | todo |
| `hub` | server `bridge/hub` (nine services) | `hub` | **done** — all nine services, plus the state model, frame encoders, prompt guards and the regex-safety validator |
| `bridge` | `bridge` worker side | `bridge` | **done** — authorization matrix, hijack coordinator, protocol contract, hijackable primitives and the worker link |
| `fanout` | server `bridge/fanout` | `fanout` | **partial** — divergence detection, group records, the in-memory store, the output collector and the controller; REST routes outstanding |
| `graphical` | graphical target registry | `graphical` | todo |
| `gui` | `vnc`, GUI ops | `gui` | todo |

### Server and edge

| Module | Python | Go | Status |
|---|---|---|---|
| `server` | server `routes/*`, `app/*`, `runtime` | `server` | todo |
| `client` | client `hijack`, `control_ws` | `client` | todo |
| `gateway` | server `gateway` | `gateway` | todo |
| `tunnel` | `tunnel`, `tunnel_invites`, `pam_tunnel` | `tunnel` | todo |
| `tunnelclient` | share/inspect client | `tunnelclient` | todo |
| `vnc` | `vnc` | `vnc` | todo |
| `shell` | `shell` | `shell` | todo |
| `manager` | External Management Tier | `manager` | todo |
| `mcp` | client `ai`/MCP (28 tools) | `mcp` | todo |
| `cli` | server CLI (`uterm`) | `cli` | todo |

### Browser

The existing `provide-uterm-frontend` (lit widgets) and `provide-uterm-app`
(React SPA) stay where they are. Two tasks belong to this port:

| Task | Status |
|---|---|
| Move both browser workspaces onto TypeScript 7 | **done** |
| React SPA covers every bootstrapped page kind | **done** |
| Port the two standalone lit entry points to React | not planned — see below |

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

## Cross-language obligations

Landing the port is not only new files. These existing artefacts have to
learn about a fourth implementation:

| Artefact | Change needed | Status |
|---|---|---|
| `spec/behavior.json` | add a `typescript` entry to `hello_defaults` | todo |
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
