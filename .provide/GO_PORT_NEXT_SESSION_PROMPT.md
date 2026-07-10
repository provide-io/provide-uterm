<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# New-session bootstrap prompt — provide-uterm Go port

> STATUS 2026-07-10: the port is functionally COMPLETE AND HARDENED — 48 Go
> packages (46 ported + the `conformance`/`interop` proof harnesses), 3
> binaries (uterm, uterm-mcp, uterm-manager), all committed on `main`. Every
> CLI subcommand is real (no stubs left); both directions of live Go↔Python
> runtime interop are proven; a Go mutation gate (gremlins) runs alongside
> the Python one. Only the follow-ups documented in .provide/HANDOFF.md
> remain. Use this prompt to pick those up.

Paste the block below to start the next session.

---

Continue the Go port of provide-uterm. Repo: /Volumes/data/pyv/provide-uterm.
Go module: packages/provide-uterm-go/ (module path
github.com/provide-io/provide-uterm/packages/provide-uterm-go, Go 1.26.5).

READ FIRST, before doing anything:
- .provide/HANDOFF.md — full status, what "compatible" means, historical
  follow-ups (all resolved), the only documented partial (PAM tunnel bridge —
  check whether it has since closed).
- packages/provide-uterm-go/README.md — package map (Python module → Go
  package), build/run for all 3 binaries, and the differential-parity
  contract.
- The auto-memory note "project-go-port" (loaded via MEMORY.md).

STATE: the port is functionally COMPLETE — 48 Go packages committed and
green (core/wire, emulation/IO, transport/bridge/client, the full server
stack including hub/serverauth/serverconfig/tunnel, gateway + tunnelclient,
cli, plus the feature-parity wave: connectors, fanout, mcp, manager, pty,
annotation), and 3 binaries (uterm, uterm-mcp, uterm-manager). Whole-module
`go build ./...`, `go test -race ./...`, and `go vet ./...` are clean; `make
quality-gate` passes at **95.3%** total coverage (floor 95.0 in the
Makefile — manager ~94.2%, server ~88%, gateway ~86%, cli ~91% carry
non-deterministic live-socket/OS-signal/fault-injection-only branches);
govulncheck reports 0 called vulnerabilities. Go 1.26.5, all deps latest. A
Go mutation gate (gremlins, `make mutation-gate`) runs over a small
pure-function perimeter (sanitizer/colors/filters/lineeditor) at
killed==100% minus 3 documented equivalents. CI runs `go-quality` (gate +
build-binaries + vuln + both interop directions) and a separate
`go-mutation-gate` job. `go build ./cmd/uterm` (or
`make build-binaries` for all three) yields working binaries whose help
trees mirror the Python CLI; every CLI subcommand — server, proxy, listen,
share, tunnel, inspect, watch, audit — is real and wired, not stubbed.
Both directions of live runtime interop are proven: Go client driving a
real `uv run uterm server` (`make interop-test`), and a real Python client
driving a real built Go `uterm server` binary
(`packages/provide-uterm-client/tests/test_go_server_interop.py`, marker
`go_interop`).

NON-NEGOTIABLE RULES (same as last session):
1. Python is the reference. Anything touching a wire format or observable
   text transform must be proven against CPython with a differential corpus
   (generate via `cd /Volumes/data/pyv/provide-uterm && uv run python <script>`
   in the scratchpad) and a deterministic subset committed under the package's
   testdata/ so CI re-verifies without Python. Byte-for-byte where the format
   demands it.
2. Per-package quality bar: aim 100% statement coverage on library/wire
   packages (integration packages may sit under the 95.0 whole-module floor
   with a documented rationale in the Makefile), gofmt clean, go vet clean,
   golangci-lint (/opt/homebrew/bin/golangci-lint) clean, `go test -race`
   clean. SPDX header block on every new .go file. Run `make quality-gate` in
   the module before declaring done; run `make mutation-gate` if touching the
   sanitizer/colors/filters/lineeditor perimeter.
3. Prefer maintained libraries over 1:1 ports where the wire format is not at
   stake — propose with reasoning before adding. Already in use:
   coder/websocket, x/crypto/ssh, provide-telemetry/go (ptel.GetLogger →
   *slog.Logger; libraries take an injectable *slog.Logger, apps call ptel;
   never import opentelemetry directly), modernc.org/sqlite, creack/pty
   (pty package + tunnelclient PTY capture), mark3labs/mcp-go (uterm-mcp),
   charmbracelet/bubbletea+lipgloss (watch TUI). Keep everything on the
   latest version and Go itself latest.
4. Use opus or sonnet for any coding subagents (pass model override). Fan out
   independent packages to parallel background agents; scope each to ONE
   directory, tell them NEVER to run whole-module `go test ./...`, and have at
   most one agent edit go.mod at a time.
5. One logical unit per commit; commit each landed package separately. Do not
   mention AI/Claude in commit messages. Shell cwd resets between Bash calls —
   `cd packages/provide-uterm-go` first for go, repo root for git.

REMAINING WORK (see HANDOFF's "Historical follow-ups" and "Documented
partial" for the authoritative, continuously-updated list — check it fresh,
this may already be smaller by the time you read it):
1. PAM tunnel bridge (`pam_tunnel` PamTunnelBridge) — depends on the
   worker-link layer; the last documented partial. Confirm current status in
   HANDOFF before starting — it may already be closed.
2. Everything else previously tracked here (hub-API-dependent server gaps,
   CLI stubs, redaction engine, browser policy rate-limits/lease-gate,
   optional MCP/PTY) is done — HANDOFF's "Historical follow-ups" section
   marks it resolved. Don't re-do it; look there first so you don't
   duplicate landed work.
3. Licensing-docs note for `vt`'s pyte provenance if/when the module ships
   (pyte is LGPL; the `vt` package is a fresh implementation — see the
   introducing commit referenced in HANDOFF).

Start by reading the docs above, confirm the module still builds and gates
pass (`cd packages/provide-uterm-go && make quality-gate`), then pick the
highest-value follow-up from HANDOFF's current list. Ask only on a genuine
scope decision; otherwise proceed.

---
