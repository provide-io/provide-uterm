<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Parity labels

One definition of the words the parity tables use. Every other matrix in this
repository links here instead of re-explaining them, so a cell means the same
thing in `docs/protocol-matrix.md`, `docs/security-language-parity.md`, the
roadmaps, and the per-package READMEs.

Last updated: 2026-08-15.

## Vocabulary

**`served`** — a **running server** of that implementation answers the
capability on its public network surface, and a live test drives it against a
real process. "Served" is about a mounted route, never about whether the code
exists.

**`unserved`** — the behavior is **implemented and tested in that language**,
but no running server of that language mounts it. The module exists; the route
does not. An unserved capability is not a security control of that
implementation: nothing an operator deploys can reach it.

**`unsupported`** — a running server **deliberately refuses** the capability,
observably and consistently. This is a decision, not a gap. Where the refusal
status is part of the contract, write it in parentheses — `unsupported (501)` —
which means an authenticated public route must return that status with no side
effect. A skipped test is not an `unsupported` cell.

**`partial`** — part of a named surface is present and the rest is not.
**Always qualify it**: name what is in and what is out, or point at the
document that does. An unqualified `partial` is a defect in the table, not a
status.

**`N/A`** — the capability **does not exist in that implementation's
architecture**, so the question does not apply: Cloudflare has no fan-out
surface at all; CF Access identity has no meaning on a self-hosted FastAPI
deployment. `N/A` is never a polite spelling of "not done yet" — that is
`unserved`, or a roadmap item.

**`Y` / `N`** — boolean answer to the row's own assertion, for rows that ask a
yes/no question rather than a servedness question. A `Y` or `N` may carry one
canonical qualifier after an em dash — `Y — unserved` — when the row is about
behavior whose reachability differs from its implementation.

## Which label to use

Walk the ladder in order and stop at the first match:

1. Does the surface exist at all in that implementation's architecture?
   No → `N/A`.
2. Does a running server answer it? Fully → `served`. Only in part →
   `partial`, qualified.
3. Does a running server refuse it deliberately and observably?
   → `unsupported`, with the status code when the status is contractual.
4. Is it implemented in that language but not mounted by its server?
   → `unserved`.
5. None of the above → it is not implemented; say so in words and link the
   roadmap. Do not reach for `N/A`.

Two rules that follow from the ladder:

- **Never mix vocabularies in one cell.** `unsupported/unserved` and
  `implemented module; unserved` say two things where the ladder says one.
  Pick the first rung that matches.
- **Never promote a library to a backend.** Completed, fully covered library
  code is evidence about a library. Only a live test against a running server
  is evidence about a backend.

## Backend status

Served server backends are **Python (FastAPI), Go, C#, and Cloudflare**.
TypeScript is a **partial** port and is `unserved` for every server surface its
Node server does not mount — the integrated set is enumerated in
`docs/typescript-port-roadmap.md`. It must not be described as a full or served
backend anywhere in this repository.

| Implementation | Server backend status | Where the claim is enforced |
|---|---|---|
| Python (FastAPI) | `served` — the reference | `live-matrix` and `multi-backend-playwright` CI jobs (`python`); the default pytest testpaths |
| Go | `served` | `live-matrix` and `multi-backend-playwright` (`go`); `go-quality`; `ci/docker_language_smoke.sh` |
| C# | `served` | `live-matrix` and `multi-backend-playwright` (`csharp`); `csharp-quality`; `ci/docker_language_smoke.sh` |
| Cloudflare (Worker + Durable Object) | `served` | `packages/provide-uterm-cloudflare/tests` in the default pytest testpaths; `.ci/check_cf_vendor_tree.sh`; the `cf` service in `docker/docker-compose.yml` |
| TypeScript (`packages/provide-uterm-ts`) | `partial` — libraries complete and differentially tested; the Node server mounts four session capabilities, the operational probes, and the REST hijack lease actions, and nothing else | `npm-quality` and `ts-mutation-gate`; deliberately **absent** from the `live-matrix` and `multi-backend-playwright` backend matrices in `.github/workflows/ci.yml` |

The TypeScript driver is registered in both roles in
`conformance/live/harness/registry.py`, so its **client** exercises the served
backends. Its server announces no named hijack or fan-out capability, so
capability-gated scenarios report `unsupported` rather than passing. The entry
criteria for adding `typescript` to the backend matrices are listed in
`docs/typescript-port-roadmap.md`.

## Where the labels are used

- [`protocol-matrix.md`](./protocol-matrix.md) — backend capability contract
  consumed by the client.
- [`security-language-parity.md`](./security-language-parity.md) — authoritative
  product scope for security controls that differ by language.
- [`typescript-port-roadmap.md`](./typescript-port-roadmap.md) — per-module
  status of the partial TypeScript port.
- [`feature-roadmap.md`](./feature-roadmap.md) — feature-level status.
- `docs/cloudflare-divergence-matrix.md` — intentional Cloudflare-vs-FastAPI
  differences, each with its pinning test.

## Keeping labels honest

A change that moves a capability between rungs — mounting a route, adding a
deliberate refusal, or landing a module that no server mounts — updates the
table cells in the same change. A cell that no longer matches the code is a
stale claim and is removed or corrected rather than left to age.
