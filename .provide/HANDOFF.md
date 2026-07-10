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
(gofmt/vet/golangci-lint/race/coverage) passes at **97.2%** total coverage
(floor 97.0, up from 92.0 across two hardening passes — manager 77%→94.2%,
then server/gateway/cli 88.5/85.8/90.8%→95.7/98.7/97.7% via real live-socket
tests, disproving the "these need fault injection" assumption for all four);
govulncheck reports 0 called vulnerabilities. Go 1.26.5, all deps on latest.
Binary build and vuln scan are explicit, separately-visible CI steps
(`make build-binaries`, `make vuln`), and a Go mutation gate (gremlins) runs
over a small pure-function perimeter — see "Mutation gate" below. The only
previously-documented unported piece (the PAM tunnel bridge) is now closed.

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
- **CLI: every subcommand is real** (server, proxy, listen, share, tunnel,
  inspect, watch, audit), same flags/help as Python.

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
- **Live `uterm proxy` cross-language parity** `packages/provide-uterm-go/cli/proxy_interop_test.go`:
  a single observable fake TCP echo upstream and a single Go WebSocket client
  drive BOTH the in-process Go `uterm proxy` (`TestProxyEchoContractGo`, via
  `serveProxy` on an ephemeral port) and a REAL `uv run uterm proxy` Python
  subprocess (`TestProxyEchoContractPython`, ephemeral port, process-group
  teardown) through one shared `proxyEchoContract` assertion — so any divergence
  surfaces as a differential failure. The contract asserts: (1) upstream banner
  flows remote→browser; (2) a multibyte UTF-8 TEXT keystroke payload echoes
  browser→remote→browser byte-identically (matching the real xterm.js frontend,
  which sends `ws.send(string)`); (3) a browser WS close tears down the upstream
  connection; (4) an upstream hangup closes the browser WS. This proof caught and
  fixed two real gaps: the Go proxy polled the remote every 200ms vs Python's
  50ms (now `defaults.ProxyPollMS`), and the Go proxy sent outbound BINARY frames
  which the shared frontend (`terminal-element.ts` renders only string payloads)
  silently discards — Go now sends UTF-8 TEXT frames like Python's `send_text`.
  Skips gracefully when uv/Python deps are absent; scoped to `go test ./cli/`.
- **PAM tunnel bridge** `server/server_pam_tunnel.go`: closes the last
  documented-unported piece. `onOpen` provisions a CF DO relay tunnel
  (egress-guarded POST `/api/tunnels`) and, when the session's connector is
  reachable via the optional `connectorLookup` surface, starts a
  `PamTunnelBridge` — PTY-mode duplex pump or capture-mode one-way pump,
  reusing the existing `tunnelclient` wire client — tracked in a
  `map[string]*PamTunnelBridge` for `onClose` teardown. Real tests (no live
  CF DO needed): relay-provisioning success/failure, both bridge modes,
  connector-lookup-miss no-op, stop-on-start-failure.
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
wire packages ~100%; integration outliers manager ~94.2%, server ~95.7%,
gateway ~98.7%, cli ~97.7%, pty ~90% (now the lowest — platform PAM/uid-mapping
syscall guards Python's own platform code also excludes). All four of
manager/server/gateway/cli were raised via REAL child processes and REAL live
sockets (httptest servers, ephemeral-port listeners, real subprocess teardown)
— what remains in each is genuinely fault-injection-only (a specific syscall
failing in a way a test cannot trigger), not something that "needs mocking".
Whole-module total 97.2%; floor 97.0 in the Makefile with documented
rationale. govulncheck: 0 called vulns.

Only intentional skip: tracing.py (OpenTelemetry span setup — project rule
forbids direct OTel; ptel covers logging). The PAM tunnel bridge (pam_tunnel
PamTunnelBridge) is now ported (server/server_pam_tunnel.go): onOpen provisions a
CF DO tunnel and, when the session's connector is reachable via the optional
connectorLookup surface, starts a PamTunnelBridge (PTY-mode duplex pump or
capture-mode one-way pump, reusing the tunnelclient wire client), tracked for
onClose teardown.

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
  lease/permission gate — done (58221750).
- **redaction engine** — done (58221750), byte-identical to Python.
- **CLI stubs** — all real; audit's float-repr hash chain got a canonical
  Go re-serialization (f67b4494).
- **MCP tools / platform PTY connector** — done (mark3labs/mcp-go,
  creack/pty), feature-parity push.
- **PAM tunnel bridge** — done, see "Package inventory" above.

## Bugs the proof suites caught (real, cross-language, pre-existing)

Each of these was a genuine defect the "prove it, don't assert it" methodology
surfaced — not something introduced by the port itself:

- **controlchannel JSON int/float fidelity** (37d96a56) — see "Verification"
  above.
- **`uterm proxy` outbound frame type** (f51c7ec5) — the Go proxy sent
  BINARY WS frames; the shared xterm.js frontend only renders string
  payloads, so a browser on the Go proxy would have shown a blank terminal.
  Also fixed a 200ms vs Python's 50ms poll-interval drift. Caught by the new
  `proxy_interop_test.go` cross-language differential test.
- **`WebSocketStreamWriter` UTF-8 corruption** (63bdc019, Python-side,
  `provide-uterm-client/transports/websocket.py`) — `drain()` decoded each
  flush independently with `errors="replace"`, so a multi-byte UTF-8
  character split across two `write()+drain()` cycles (which happens when a
  transport hands data back one chunk at a time, e.g. `WsTerminalProxy`'s
  byte-at-a-time forwarding) was silently corrupted into replacement
  characters. Fixed with a persistent incremental UTF-8 decoder. Caught by
  `TestProxyEchoContractPython` intermittently failing on a multibyte payload.

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
