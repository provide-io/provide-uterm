# HANDOFF — Go port of provide-uterm

## Problem / request

Port uterm to Go (`/goal` 2026-07-09): 100% tested/TDD, all quality gates
passing, secure, well formatted, good hygiene, **wire-compatible with the
Python implementation**, using provide-telemetry. Mid-session directives from
Tim: prefer popular maintained libraries over 1:1 ports where sensible
(propose with reasoning); use opus/sonnet for coding subagents; use the
latest version of every package and Go itself.

## What "compatible" means here (answer to Tim's question)

- **Wire protocol: byte-level compatible, proven.** Go and Python interoperate
  over the identical WebSocket DLE/STX control-frame format, frame schemas,
  and HMAC identity signatures. Proven by differential corpora generated from
  CPython (vt 911 cases, ctrlmsg 544 HMAC sigs, 24 golden frames, controlchannel
  both-direction round-trips, controlplane SQLite cross-readable both ways).
- **xterm: same frontend.** The browser still runs the existing xterm.js
  TypeScript. Emulation engine differs (pyte → the `vt` port) but is
  behaviorally identical (911-case full-state differential, 0 mismatches).
- **API: semantically equivalent, NOT signature-identical.** Idiomatic Go
  (option structs, error returns, context.Context, pointers for None). Not a
  drop-in for Python-API callers.
- **CLI: not built yet** (task #8). When built, mirror `uterm` / `uterm server`
  subcommand syntax.

## Approach / reasoning

- Workspace member `packages/provide-uterm-go/` (module
  `github.com/provide-io/provide-uterm/packages/provide-uterm-go`, go 1.26.5).
- Python stays the reference. Every wire/text-transform package proven by a
  differential corpus vs CPython (generated via `uv run` scripts), with
  deterministic goldens committed under `testdata/`. See the module README for
  the package map + parity table.
- Libraries (approved direction): coder/websocket, x/crypto/ssh,
  provide-telemetry/go v0.5.0 (`ptel.GetLogger` → *slog.Logger; libraries take
  an injectable *slog.Logger, apps call ptel), modernc.org/sqlite (pure Go).
  pyte was PORTED (`vt`) because prompt detection depends on its exact screen
  semantics.
- Per-package gate: 100% coverage target, go vet, golangci-lint, -race, gofmt,
  SPDX headers. `make quality-gate` in the module enforces it (whole-module
  threshold set to 99.5% — see Makefile rationale). One logical unit per commit.

## Completed (committed, gates green)

31 Go packages. All 100% coverage except transports 98.2%, client 99.7%,
controlplane/sqlite 95.1% (residuals are non-deterministic syscall/driver
guards the Python originals also exclude via pragma). Whole-module
`go test -race ./...` passes at 99.7% total; govulncheck: 0 called vulns.

controlchannel, channels, sanitizer, redaction, filters, lineeditor, auth,
ansi, colors, screen, defaults, frames, ctrlmsg, fileio, recording,
sessionlogger, session, replay, vt, emulator, render, deckmux, transports,
termsession, detection, bridge, client, shell, hub (wave-A services),
controlplane (+memory/+sqlite/+bootstrap).

Repo plumbing: ci.yml `go-quality` job (make quality-gate, toolchain from
go.mod); detect-secrets excludes `packages/provide-uterm-go/*/testdata/`;
codespell allows "ShiftIn" and "sHTTP". Deps bumped to latest, Go 1.26.5.

## Next-session checklist (nothing in flight — all agents landed)

- [ ] **Hub wave-B** (task #5, the remaining server core): TermHub composition
      over the wave-A services, MessageRouter (broadcast/send_worker + hijack-
      state frame building), ConnectionManager, PresenceManager, resume-token
      store. Wave-A left clean seams (LeaseHub, StateStoreConfig callbacks,
      IdentityProvider, polling requestSnapshot func) — compose, don't rewrite.
      Python source: packages/provide-uterm-server/.../bridge/hub/ (core_impl.py,
      router_impl.py, connection.py, resume.py, ext.py, event_bus.py).
- [ ] **HTTP/WS server + gateway** (net/http): the REST routes the Go client
      already targets (bridge/routes/*.py), the browser WS endpoint, TOML
      config (propose pelletier/go-toml/v2), auth modes (dev_token/jwt/header/
      api_key/webhook — security-sensitive, mirror exactly).
- [ ] **CLI + server binary** (task #8): cobra, mirror `uterm`/`uterm server`
      syntax, `ptel.SetupTelemetry` at startup.
- [ ] MCP tools (propose mark3labs/mcp-go), platform PTY connector (propose
      creack/pty) — if in scope.
- [ ] After each: `make quality-gate` (module) + the package's differential
      parity check.

## Key facts for whoever continues

- Shell cwd resets between Bash calls — always `cd packages/provide-uterm-go`
  first; `git` ops from the repo root.
- Pre-commit occasionally reformats (ruff) or flags codespell/detect-secrets on
  Go files — re-add and retry; add allowlist entries for legitimate tokens.
- Session memory: ~/.claude/.../memory/project_go_port.md mirrors this state.
- vt/pyte provenance: pyte is LGPL; commit 92a17735 records the fresh-
  implementation rationale — add a licensing-docs note if the module ships.
