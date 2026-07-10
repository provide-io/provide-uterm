<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# New-session bootstrap prompt — provide-uterm Go port

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

STATE: 31 Go packages are ported, committed, and green — controlchannel,
channels, sanitizer, redaction, filters, lineeditor, auth, ansi, colors,
screen, defaults, frames, ctrlmsg, fileio, recording, sessionlogger, session,
replay, vt (pyte port), emulator, render, deckmux, transports, termsession,
detection, bridge (worker link), client (REST + control WS), shell, hub
(wave-A services), controlplane (memory + sqlite + bootstrap). Whole-module
`go test -race ./...` passes at 99.7% total coverage; `make quality-gate`
(in the Go module) passes; deps are on latest and Go is 1.26.5. A CI
`go-quality` job runs the gate.

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

REMAINING WORK (in priority order — see HANDOFF checklist for detail):
1. Hub wave-B: compose TermHub over the wave-A services (clean seams already
   exist: LeaseHub, StateStoreConfig callbacks, IdentityProvider, polling
   requestSnapshot func), then MessageRouter (broadcast/send_worker +
   hijack-state frame building), ConnectionManager, PresenceManager, resume-
   token store. Python: packages/provide-uterm-server/.../bridge/hub/
   (core_impl.py, router_impl.py, connection.py, resume.py, ext.py,
   event_bus.py).
2. HTTP/WS server + gateway over net/http: the REST routes the Go client
   already targets (bridge/routes/*.py), the browser WS endpoint, TOML config
   (propose pelletier/go-toml/v2), auth modes (dev_token/jwt/header/api_key/
   webhook — security-sensitive, mirror exactly, reuse the Go auth package).
3. CLI + server binary: cobra, mirror the Python `uterm` / `uterm server`
   subcommand syntax so commands match, `ptel.SetupTelemetry` at startup.
4. Optional if in scope: MCP tools (propose mark3labs/mcp-go), platform PTY
   connector (propose creack/pty).

Start by reading the three docs above, then confirm the module still builds
and gates pass (`cd packages/provide-uterm-go && make quality-gate`), then
begin hub wave-B. Ask me only if a genuine scope decision comes up; otherwise
proceed.

---
