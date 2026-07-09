# HANDOFF — Go port of provide-uterm

## Problem / request

Port uterm to Go (`/goal` 2026-07-09), with: 100% tested/TDD, all quality
gates passing, secure, well formatted, good hygiene, **wire-compatible with
the Python implementation**, and using the provide-telemetry library.
Follow-ups from Tim mid-session: prefer popular maintained libraries over
1:1 ports where sensible (propose with reasoning), and use opus/sonnet
models for coding subagents.

## Approach / reasoning

- New workspace member `packages/provide-uterm-go/` (Go module
  `github.com/provide-io/provide-uterm/packages/provide-uterm-go`, go 1.26).
- Python stays the reference. Every wire/text-transform package is proven by
  a **differential corpus vs CPython** (generated via `uv run` scripts),
  with deterministic goldens committed under `testdata/` so Go CI re-checks
  parity without Python. See packages/provide-uterm-go/README.md for the
  package map and parity table.
- Library choices (user-approved direction): coder/websocket (WS client),
  x/crypto/ssh (SSH), provide-telemetry/go v0.5.0 (published;
  `ptel.GetLogger(ctx, name)` returns *slog.Logger — libraries take an
  injectable *slog.Logger, apps call ptel directly), modernc.org/sqlite
  (pure-Go, for the control plane). Exception: pyte was PORTED (package
  `vt`) because prompt-detection parity depends on pyte's exact screen
  semantics — proven with a 911-case full-state differential (0 mismatches).
- Quality bar per package: 100% statement coverage, go vet, golangci-lint,
  -race, gofmt, SPDX headers. `make quality-gate` in the Go module enforces
  all of it (fmt-check/vet/lint/race/cover-100%).
- One logical unit per commit; every landed package committed separately.

## Completed (all 100% coverage, race- and lint-clean, committed)

controlchannel (+latin-1 shim), channels, sanitizer, redaction, filters,
lineeditor, auth, ansi, colors, screen (+CP437), defaults, frames
(golden-parity incl. extras policies), ctrlmsg (CPython-exact canonical JSON
→ HMAC signature parity, 544-case corpus), fileio, recording, sessionlogger,
session (io+expect), replay, vt (pyte port), emulator, render (palette/SGR/
segments/buffer/image), deckmux, transports (98.2% — residual lines are
non-deterministic syscall branches; documented), termsession
(telnet+WS sessions, loopback + live echo-server e2e).

Repo plumbing: detect-secrets excludes `packages/provide-uterm-go/*/testdata/`
(synthetic golden vectors); codespell allows "ShiftIn".

## In flight (background agents, opus)

- detection/ — engine port with differential corpus (resumed once after an
  API stall; 50/50 differential cases were already passing).
- bridge/ — worker_link (TermBridge), contracts version negotiation,
  coordinator; fake-hub WebSocket e2e.
- controlplane/ — memory + sqlite engines/stores, **cross-compatible DB
  files with Python** (both-direction round-trip tests), modernc.org/sqlite.

## Next-session checklist

- [ ] Land + commit the three in-flight agent packages (verify gates first).
- [ ] Hub/server core (task #5, ~5.8k lines): split into (a) services
      (registry/limiter/approval/lease/state), (b) router/connection/
      presence/polling, (c) net/http server + WS endpoint + TOML config +
      auth modes. Reuse frames/controlchannel/bridge contracts.
- [ ] shell/ (~1.5k lines) — decide shape of the Go REPL port.
- [ ] uterm-go CLI + server binary (cobra + pelletier/go-toml/v2 proposed),
      ptel.SetupTelemetry at app startup.
- [ ] Client HTTP/WS consumer lib + MCP tools + platform PTY connector
      (creack/pty proposed) if scope includes them.
- [ ] CI job for the Go module (mirror `make quality-gate`), root docs
      mention, licensing note for the vt/pyte provenance (pyte is LGPL;
      commit 92a17735 records the fresh-implementation rationale).
- [ ] Full-module `go test -race ./...` + quality-gate once no agents are
      mid-write.

## Key facts for whoever continues

- Never run whole-module `go test ./...` while agents are writing partial
  packages; test per-package from the module root (shell cwd resets between
  commands — always `cd packages/provide-uterm-go` first).
- Pre-commit stash warnings ("Unstaged files detected") during commits are
  the concurrent agents' worktrees — harmless; retry once if a hook claims
  "files were modified".
- go.mod is edited by at most one agent at a time (currently controlplane).
- Session memory: ~/.claude/.../memory/project_go_port.md tracks the same
  state. The previous HANDOFF content (kbdint/webhook live tests) was
  completed work from an earlier session; see git history of this file.
