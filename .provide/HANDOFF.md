# HANDOFF — Go port of provide-uterm (complete)

## Problem / request

Port uterm to Go (`/goal` 2026-07-09): 100% tested/TDD, all quality gates
passing, secure, well formatted, good hygiene, **wire-compatible with the
Python implementation**, using provide-telemetry. Mid-session directives:
prefer popular maintained libraries over 1:1 ports where sensible; use
opus/sonnet for coding subagents; use the latest version of every package
and Go itself.

## Status: the port is functionally complete + hardened

50 Go packages, 3 binaries (uterm, uterm-mcp, uterm-manager) committed on
`main`. Whole-module `go build ./...`, `go test -race ./...`, and
`go vet ./...` are clean; `make quality-gate`
(gofmt/vet/golangci-lint/race/coverage) passes at **95.3%** total coverage
(floor 95.0, up from 92.0 — manager rose 77%→94.2% this pass); govulncheck
reports 0 called vulnerabilities. Go 1.26.5, all deps on latest. Binary build
and vuln scan are now explicit, separately-visible CI steps
(`make build-binaries`, `make vuln`), and a Go mutation gate (gremlins) runs
over a small pure-function perimeter — see "Mutation gate" below.

## What "compatible" means (answer to Tim's question)

- **Wire protocol: byte-level, proven.** Go ↔ Python interoperate over the
  same DLE/STX control channel, frame schemas, HMAC identity + webhook
  signatures, REST routes, and cross-readable SQLite. Proven by CPython-
  generated differential corpora (vt 911 cases, ctrlmsg 544 HMAC sigs, 24
  golden frames, serverauth HMAC/JWT goldens, controlplane SQLite both-
  direction). An in-process server e2e runs a real Go worker ↔ hub ↔ browser.
- **xterm: same frontend** (existing xterm.js TS); emulation is the `vt`
  pyte-port, behaviorally identical.
- **API: semantically equivalent, idiomatic Go** (option structs, error
  returns, context.Context), not signature-identical.
- **CLI: `uterm server` + `uterm proxy` fully wired**, same flags/help as
  Python. Tunnel/TUI subcommands stubbed (see below).

## Verification / proofs (runnable)

- **Conformance suite** `packages/provide-uterm-go/conformance/`: the single
  authoritative Go↔Python gate. `gen_vectors.py` emits authoritative
  input→output pairs from the Python reference; the Go test replays each and
  asserts byte-agreement, **regenerating live from Python (uv) when reachable**
  else the committed golden. Surfaces covered: control-frame encode/decode,
  terminal-data framing, screen normalize + CP437, ansi normalize + 256
  upgrade, webhook HMAC, identity HMAC signature, **DeckMux identity
  (name/color/initials hash)**, emulator snapshot. Run:
  `cd packages/provide-uterm-go && go test ./conformance/` (add
  `UTERM_CONFORMANCE_NO_REGEN=1` to force the golden path).
- **Bug the suite caught + fixed** (37d96a56): controlchannel decoded wire
  JSON numbers to float64, losing Python's int/float distinction, so
  identity-frame HMAC signatures diverged for integer claims. Fixed with
  json.Number end-to-end; verified Go and Python produce the identical HMAC
  signature for an integer-claim identity.
- **Live server** (demonstrated): `uterm server` binds, serves `/api/health`
  200, gates unauth `/api/sessions` 401, dev_token mints an HS256 JWT that
  unlocks `/api/sessions` + `/api/metrics` 200; provide.telemetry emits
  structured logs with the token masked.
- **Live Go↔Python runtime interop** `packages/provide-uterm-go/interop/`: the
  one proof that is neither offline nor demonstrated-by-hand but an automated
  test driving a REAL Python server from the Go client over the real wire. It
  starts an actual `uv run uterm server` subprocess (ephemeral loopback port,
  dev_token auth, `UTERM_API_ONLY=1`), polls `/api/health` until ready, reads
  the minted JWT back from `UTERM_DEV_TOKEN_PATH`, then with the Go
  `client.HijackClient`/`client.ControlWSClient`: (1) REST — authenticated
  `/api/health` + `/api/sessions` listing, then the full operator hijack lease
  round-trip (mode → acquire → send → snapshot → release) asserting the sent
  marker echoes into the transcript; (2) WebSocket — dials the browser control
  WS, consumes the hello handshake frame, sends an input frame and reads the
  echoed screen back over the inline DLE/STX channel. Skips gracefully when uv
  or the Python deps are absent (mirrors the conformance skip); tears the
  subprocess down via a process-group SIGTERM→SIGKILL fallback. Kept OUT of the
  Go quality-gate (needs Python): `make -C packages/provide-uterm-go
  interop-test`, wired into CI's `go-quality` job with its own uv+python setup.
- **Live Python↔Go runtime interop (reverse direction)**
  `packages/provide-uterm-client/tests/test_go_server_interop.py`: the mirror of
  the above — an automated test driving a REAL Go `uterm server` binary from the
  REAL Python client library over the real wire. It builds `./cmd/uterm` fresh
  into a pytest tmp dir, launches it (ephemeral loopback port, dev_token auth via
  a `--config` TOML that also pins a test worker-bearer token, JWT read back from
  `UTERM_DEV_TOKEN_PATH`), polls `/api/health`, then with the Python
  `HijackClient`/`connect_async_ws`: (1) REST — authenticated `/api/health` +
  `/api/sessions` listing (asserts `provide-shell` present), then the full
  operator hijack lease round-trip (input mode → acquire → send → snapshot →
  release) asserting the sent marker echoes into the snapshot; (2) WebSocket —
  dials the browser control WS, consumes the hello frame, sends an input frame,
  reads the echo back over the inline DLE/STX channel. Documented deviation: the
  Go server's `provide-shell` reference session is an in-process registry
  connector (not a hub worker), so the test attaches a real Python *worker* to
  the Go hub over `/ws/worker/…` and drives Python-worker ↔ Go-hub ↔
  Python-browser — a strictly richer proof. Marked `@pytest.mark.go_interop`,
  skips gracefully when the Go toolchain is absent, tears the subprocess down via
  a process-group SIGTERM→SIGKILL fallback, and runs in CI's `go-quality` job
  (which already has both Go and the synced Python env). BOTH interop directions
  are now live-proven.
- **Live DeckMux deck** (demonstrated + now pinned in conformance): 3 users
  join a deck → distinct hash-generated names/colors/initials, presence_update
  broadcast, control_request → control_transfer, disconnect → presence_leave.
  Python confirmed the Go-generated names/colors/initials match for the same
  user ids and that every Go-emitted deck frame validates under the Python
  Pydantic schema (PresenceSync/Update/Leave, ControlTransfer).

## Mutation gate (Go side)

Added to mirror the Python `scripts/run_mutation_gate.py` rigor. Tool:
[gremlins](https://github.com/go-gremlins/gremlins) v0.6.0, invoked via
`go run` (no `go.mod` entry — same pattern as golangci-lint/govulncheck).
Perimeter (small on purpose — mutation testing recompiles+reruns per mutant):
`sanitizer`, `colors`, `filters`, `lineeditor`, `redaction`, `channels`,
`frames` — pure-function packages already at ~100% coverage with real
branch/boundary/arithmetic logic. Score: **212 mutants killed, 0 unexcused
survivors, 4 documented-equivalent** (100% of the non-equivalent perimeter).
Run: `cd packages/provide-uterm-go && make mutation-gate` (needs `python3` >=
3.11; driver `ci/mutation_gate.py`, stdlib only). Fails on any unexcused LIVED /
NOT_COVERED / TIMED_OUT mutant.

- Perimeter expansion (2026-07-10): added `redaction` (clean, 3 killed),
  `channels`, and `frames`. Two real assertion gaps fixed with tests: channels
  `negotiate` `version > 0` (the existing "zero" channel was unsupported so the
  `ok &&` short-circuit hid the boundary — made it a supported channel so the
  "not granted at version 0" assertion is load-bearing), and frames `nowTS`
  `/1e9` unit conversion (the `tsIsNow` helper only lower-bounded the stamp;
  widened to a two-sided window so a `*1e9` mutant no longer survives). Also
  closed a channels coverage gap first (the `json.Number` fractional-version
  reject path in `coerceChannelMap`) so the package is a true 100% before
  mutating. One new documented-equivalent: frames `extras.go` marshalWithExtras
  `make(map, len(known)+len(extra))` -> `-` — the map capacity is only a sizing
  HINT (a negative runtime hint is clamped, not a panic), so contents are
  byte-identical. NOTE: `ansi` and `ctrlmsg` were evaluated and DEFERRED — their
  `switch` cases carry comparison expressions (`case 30 <= code && code <= 37`),
  and Go's coverage profile marks only the case BODY covered, not the
  case-condition position, so gremlins reports those (fully-tested) mutants as
  NOT_COVERED — a tooling artifact the gate treats as FAIL and cannot excuse.
  Pick switch-with-`if`-body packages, not comparison-in-case ones, when
  extending further.

- The run surfaced 9 real assertion gaps that coverage had missed, all fixed
  with tests: sanitizer `~` (0x7E) printable-boundary; colors distance-tie
  first-wins parity (RGBTo16Index(0,92,230)==1, matches CPython), 9-vs-10-digit
  parseComponent saturation, and the `i+4 < n` truecolor-run bounds guard;
  filters CSI final-byte edges `@`/`~`; lineeditor backspace cursor decrement +
  Ctrl+W scan loops reaching buffer index 0.
- The 3 allowlisted equivalents (`packages/provide-uterm-go/mutation_equivalents.toml`)
  are `<`/`>` vs `<=`/`>=` boundary flips where both branches return the same
  value: sanitizer `len(filtered) <= maxBytes` (`filtered[:maxBytes]==filtered`
  at equality) and the two clamp8 edges (clamp of 0 / 255).
- IMPORTANT: gremlins derives each mutant's timeout from the tiny baseline test
  time; too-low `--timeout-coefficient` spuriously times out mutants (masking
  real survivors). The gate uses 100 (zero timeouts locally) and FAILS on any
  timeout so a slow runner surfaces loudly. CI job: `go-mutation-gate` in
  `.github/workflows/ci.yml` (separate from `go-quality`).

## Package inventory (all committed, gates green)

Core/wire: controlchannel, channels, sanitizer, redaction, filters,
lineeditor, auth, ansi, colors, screen, defaults, frames, ctrlmsg.
Emulation/IO: vt, emulator, render, session, sessionlogger, recording,
fileio, replay, detection, deckmux, shell.
Transport/bridge/client: transports, termsession, bridge, client.
Server: controlplane(+memory/+sqlite/+bootstrap), hub (wave-A services +
wave-B TermHub/router/connection/presence/resume + ResolveApproval),
serverauth (5 auth modes), serverconfig (TOML), server (net/http +
coder/websocket, DeckMux presence + input-approval gating + tunnel
invite/token lifecycle live), tunnel (invite/token, BLAKE2b hash-parity).
CLI/gateway: gateway (telnet/SSH listener), tunnelclient (share/tunnel/
inspect), cli + cmd/uterm.
Feature parity (2026-07-10): connectors (real shell/ssh/telnet/websocket
sessions), fanout (difflib-exact divergence controller, wired to browser-WS +
MCP), mcp (uterm-mcp binary, ~21 tools), manager (uterm-manager fleet binary),
pty (platform PTY/PAM/uid), annotation, server egress guard (SSRF) +
discovery + recording routes + PAM.

50 packages, 3 binaries (uterm, uterm-mcp, uterm-manager). Coverage: library/
wire packages ~100%; integration outliers manager ~94.2% (real-child-process
monitor-loop tests added this pass; residual is syscall-fault-injection-only:
rename/write/MkdirAll failures, log rotation), server ~88, gateway ~86,
cli ~91 (live-socket / TTY / multi-process / OS-signal branches). Whole-module
total 95.3%; floor 95.0 in the Makefile with documented rationale. govulncheck:
0 called vulns.

Only intentional skip: tracing.py (OpenTelemetry span setup — project rule
forbids direct OTel; ptel covers logging). Documented partial: PAM tunnel
bridge (pam_tunnel PamTunnelBridge — depends on the worker-link layer).

CLI status: ALL subcommands now real — server, proxy, listen, watch, audit,
and share/tunnel/inspect (via the tunnelclient package: byte-faithful frame
codec with live wire-parity vs Python, coder/websocket client, creack/pty
PTY capture, http-proxy + intercept). Proven: share streams a real PTY to a
tunnel WS, tunnel forwards a local TCP port, inspect reverse-proxies HTTP with
inspection + intercept-drop.

Output redaction + browser-WS per-frame rate limits + the lease/permission
gate are all live (58221750): the StreamRedactor is byte-identical to Python
over the default rule set; browser input/control frames are token-bucketed
(rate_limited error + drop on exceed), and non-owner / expired-lease inputs
are dropped at the worker-forward point. **All follow-ups from the earlier
list are now done.**

## Historical follow-ups (all resolved)

- **browser policy remainder**: per-frame token-bucket rate limits and the
  lease/permission gate (prepare_browser_input) on the browser WS — DeckMux +
  approval hold are live; these two are still omitted.
- **redaction engine** (hub left an OutputPolicyGate + Redactor seam) — port
  when the redaction Python modules are needed.
- **CLI stubs**: listen/share/tunnel/inspect/watch (need the tunnel-client /
  gateway-listener / TUI ports) and audit (Python's float-repr hash chain is
  not byte-reproducible in Go — needs a canonical re-serialization decision).
- **Optional**: MCP tools (propose mark3labs/mcp-go), platform PTY connector
  (propose creack/pty).

## Key facts for whoever continues

- Shell cwd resets between Bash calls — `cd packages/provide-uterm-go` first;
  git from the repo root.
- Pre-commit reformats (ruff) and flags codespell/detect-secrets on Go files:
  add `// pragma: allowlist secret` (or `# pragma...` inside TOML fixtures)
  for legitimate test tokens; codespell ignore-words holds shiftin/shttp/
  defint. Re-add + retry after a hook auto-fix.
- CI `go-quality` job runs `make quality-gate`, `make build-binaries`,
  `make vuln`, `make interop-test` (Go→Python), and the reverse
  Python→Go interop pytest. `go-mutation-gate` is a separate CI job.
- vt/pyte provenance: pyte is LGPL; commit 92a17735 records the fresh-
  implementation rationale — add a licensing-docs note if the module ships.
- Session memory `project-go-port` mirrors this state;
  GO_PORT_NEXT_SESSION_PROMPT.md is a paste-ready bootstrap.
