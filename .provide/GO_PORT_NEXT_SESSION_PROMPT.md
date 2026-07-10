<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# New-session bootstrap prompt — provide-uterm Go port

> STATUS 2026-07-09: the port is functionally COMPLETE — 38 packages, the
> `uterm` binary builds, `uterm server`/`uterm proxy` are wired, whole-module
> gates pass. Only the documented follow-ups in .provide/HANDOFF.md remain
> (hub-API-dependent server gaps, CLI stubs, optional MCP/PTY). Use this
> prompt to pick those up.

Paste the block below to start the next session.

---

Continue the Go port of provide-uterm. Repo: /Volumes/data/pyv/provide-uterm.
Go module: packages/provide-uterm-go/ (module path
github.com/provide-io/provide-uterm/packages/provide-uterm-go, Go 1.26.5).

READ FIRST, before doing anything:
- .provide/HANDOFF.md — full status, what "compatible" means, next-session
  checklist.
- packages/provide-uterm-go/README.md — package map (Python module → Go
  package) and the differential-parity contract.
- The auto-memory note "project-go-port" (loaded via MEMORY.md).

STATE: the port is functionally COMPLETE — 38 Go packages committed and
green, including the full server stack (controlplane, hub wave-A+B, serverauth,
serverconfig, server HTTP/WS) and the `uterm` binary (cli + cmd/uterm).
Whole-module `go build ./...`, `go test -race ./...`, and `go vet ./...` are
clean; `make quality-gate` passes at 97.1% total coverage (floor 95% — server
~85% and cli ~96% carry non-deterministic live-socket/OS-signal branches);
govulncheck 0 called. Go 1.26.5, all deps latest. A CI `go-quality` job runs
the gate. `go build ./cmd/uterm` yields a working binary whose help tree
mirrors the Python CLI; `uterm server` and `uterm proxy` run end to end.

NON-NEGOTIABLE RULES (same as last session):
1. Python is the reference. Anything touching a wire format or observable
   text transform must be proven against CPython with a differential corpus
   (generate via `cd /Volumes/data/pyv/provide-uterm && uv run python <script>`
   in the scratchpad) and a deterministic subset committed under the package's
   testdata/ so CI re-verifies without Python. Byte-for-byte where the format
   demands it.
2. Per-package quality bar: aim 100% statement coverage, gofmt clean, go vet
   clean, golangci-lint (/opt/homebrew/bin/golangci-lint) clean, `go test
   -race` clean. SPDX header block on every new .go file. Run `make
   quality-gate` in the module before declaring done.
3. Prefer maintained libraries over 1:1 ports where the wire format is not at
   stake — propose with reasoning before adding. Already in use:
   coder/websocket, x/crypto/ssh, provide-telemetry/go v0.5.0 (ptel.GetLogger
   → *slog.Logger; libraries take an injectable *slog.Logger, apps call ptel;
   never import opentelemetry directly), modernc.org/sqlite. Keep everything
   on the latest version and Go itself latest.
4. Use opus or sonnet for any coding subagents (pass model override). Fan out
   independent packages to parallel background agents; scope each to ONE
   directory, tell them NEVER to run whole-module `go test ./...`, and have at
   most one agent edit go.mod at a time.
5. One logical unit per commit; commit each landed package separately. Do not
   mention AI/Claude in commit messages. Shell cwd resets between Bash calls —
   `cd packages/provide-uterm-go` first for go, repo root for git.

REMAINING WORK (all optional/scoped follow-ups — see HANDOFF for detail):
1. Hub-API-dependent server gaps: approval command re-injection +
   `approval_resolved` broadcast (needs an InMemoryApprovalStore iterator +
   ResolveApproval facade on the hub package), the tunnel invite/token
   lifecycle, and the full browser policy pipeline (input approval/hold,
   DeckMux fan-out, per-frame rate limits). The `server` package covers the
   interop surface; these extend it.
2. CLI stubs to flesh out: listen (gateway listener), share/tunnel/inspect
   (tunnel client), watch (TUI), audit (blocked on a canonical re-
   serialization decision — Python's float-repr hash chain is not
   byte-reproducible in Go). Each already registers the matching flag surface.
3. Redaction engine + tunnel wire-framing (the hub left OutputPolicyGate /
   Redactor / TunnelSender seams).
4. Optional: MCP tools (propose mark3labs/mcp-go), platform PTY connector
   (propose creack/pty).

Start by reading the docs above, confirm the module still builds and gates
pass (`cd packages/provide-uterm-go && make quality-gate`), then pick the
highest-value follow-up. Ask only on a genuine scope decision; otherwise
proceed.

---
