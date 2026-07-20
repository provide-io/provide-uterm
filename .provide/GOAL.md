# Goal — Cross-language server parity (post residual closeout 1→5)

**Status:** phases 1–3 landed; phase 4 optional  
**Head baseline:** `main` @ inspect/tunnel parity (`aeed611a`+) — multi-backend hub green  
**Out of scope (explicit):** **C# MCP** (`uterm-mcp`) — not required; keep README de-scope  
**Order:** phases **1 → 2 → 3 → 4** sequentially; each phase ends CI-green on `origin/main` + `.provide/HANDOFF.md` update

---

## Objective (paste for `/goal`)

```
Execute remaining provide-uterm Python/Go/C# server parity after residual closeout 1→5.
C# MCP is out of scope permanently for this goal (do not implement uterm-mcp for C#).

PHASE 1 — Terminal proxy parity OR formal de-scope
- Today: test_terminal_proxy is the only multi-backend Playwright product-gap skip
  (Python fastapi_utils.WsTerminalProxy + mount_terminal_ui + telnet echo).
- Prefer: port minimal production surface to Go and C# (WS raw term → telnet/upstream
  echo path equivalent to WsTerminalProxy) and dual-mode multi-backend e2e green on
  python|go|csharp.
- Acceptable alternate: document permanent de-scope in HANDOFF + README with reason
  (embedding helper, not hub) and keep explicit skip reason strings (never empty pass).
- Done when: either 3-backend multi-backend green for terminal_proxy, or HANDOFF marks
  permanent de-scope with no “TODO port” ambiguity; CI still green.

PHASE 2 — C# control-plane REST toward Go/Python host API
- Close the largest C# server gap vs Go: session lifecycle beyond CRUD, webhooks,
  fan-out groups API, and (as needed for operators) profiles / session
  connect-disconnect-restart-mode-clear-analyze parity where Go already has routes.
- Prefer real endpoints + dual-mode or multi-backend HTTP/WS tests over forever-stubs.
- Explicit skip only for hard product gaps with reason strings.
- Done when: multi-backend (or dedicated interop) exercises the new C# routes; HANDOFF
  table lists C# REST surface vs Go; CI csharp-quality + multi-backend green.

PHASE 3 — Tunnel host lifecycle on C# (not just /tunnel WS + client)
- Today: all three have binary /tunnel/{id} WS + inspect e2e; Go/Python also host
  POST/GET /api/tunnels, token rotate/revoke, share /s/{id}.
- Port enough C# host REST that `uterm share`/`inspect` can target a pure C# hub
  for the same happy path as Go (create tunnel → worker token → /tunnel WS → share page).
- Done when: multi-backend or API e2e proves C# tunnel minting + inspect path without
  requiring Python as tunnel host; HANDOFF updated.

PHASE 4 — Quality ratchet (optional depth, not MCP)
- C# COVER_THRESHOLD raise toward ~98.5–99 only with ≥0.2pt headroom on BOTH
  csharp-quality and csharp-quality-windows (no residual-exclusion gaming).
- Optional: grow mutation perimeters one pure module/language at a time (killed==100).
- Optional: reduce multi-backend page.route dependency by serving production UI
  assets from Go/C# servers the same way Python does (full SPA hosting).
- Done when: chosen sub-items green on main; HANDOFF lists floors/modules.

Global constraints: small commits; real gates (multi-backend-playwright, package
quality/mutation); push origin/main; clean tree; TDD for flaky multi-backend; no
empty multi-backend passes; no C# MCP work; update .provide/HANDOFF.md after each phase.

Order: 1 terminal-proxy decision/port → 2 C# control-plane REST → 3 C# tunnel host
REST → 4 cover/mutation/UI hosting depth.
```

---

## Already at multi-backend parity (do not re-open)

| Surface | python | go | csharp |
|---------|--------|-----|--------|
| Hijack / chaos | yes | yes | yes |
| DeckMux pin/control/leave | yes | yes | yes |
| Resume | yes | yes | yes |
| Color static | yes | yes | yes |
| Inspect + `/tunnel` HTTP frames | yes | yes | yes |
| Reconnect spinner / control decoder | frontend harness (all matrix cells) | same | same |

---

## Explicit non-goals

| Item | Reason |
|------|--------|
| **C# MCP / `uterm-mcp`** | Operator de-scope; user confirmed not needed |
| Cloudflare Worker full port to Go/C# | Separate product line |
| Absolute 100% cover on live PTY/SSH/FxSsh/RFB without live harness | Residual policy |
| Removing in-process Python PT fast path | Keep default CI speed |

---

## Phase detail

### Phase 1 — Terminal proxy

**Today:** `test_terminal_proxy.py` + `skip` on go/csharp.

**Prefer port (recommended if embedding story matters):**
- Go: small `WsTerminalProxy`-like handler or document existing CLI proxy vs missing library mount
- C#: same
- Dual-mode fixtures + multi-backend-playwright step includes module on all backends

**Or de-scope:** HANDOFF + csharp/go README one-liners: “terminal UI proxy is Python fastapi_utils only.”

### Phase 2 — C# control-plane REST

Priority routes (mirror Go `routes_*.go` / Python):

1. Session ops: connect / disconnect / restart / mode / clear / analyze / snapshot / events  
2. Webhooks: register / list / delete  
3. Fan-out: groups create/list/delete/send/grants  
4. Profiles (if load-bearing for operators)

Tests: dual-mode HTTP client tests against `spawn_backend_server` + existing auth/TEST_MODE patterns.

### Phase 3 — C# tunnel host lifecycle

Parity with Go `routes_tunnels_full.go` + Python `routes/tunnels.py`:

- `POST /api/tunnels`, rotate/revoke tokens, list  
- Share consumer `/s/{id}` → inspect/session redirect  
- Wire `Tunnel` store already in C# types  

Proof: API + existing inspect e2e still green; optional CLI smoke against C# hub only.

### Phase 4 — Quality / hosting depth

- **Cover:** Wave harnesses → remeasure Ubuntu+Windows → raise floor only with headroom  
- **Mutation:** one pure module per language if tests pin  
- **UI hosting:** Map static frontend from C# (and ensure Go frontend-dir path) so inspect/hijack need fewer page.routes  

---

## Verification plan

1. After each phase: `multi-backend-playwright` matrix green (python/go/csharp); relevant package quality jobs green.  
2. Phase 1: either terminal_proxy not skipped on go/csharp, or HANDOFF permanent de-scope.  
3. Phase 2–3: HANDOFF route table C# vs Go updated; no empty multi-backend passes.  
4. Phase 4: COVER_THRESHOLD / mutation PERIMETER documented; dual-OS rule honored.  
5. Final: clean `origin/main`; **no** C# MCP commits in the series.

---

## Suggested `/goal` one-liner

> Close remaining Py/Go/C# **server** parity: terminal-proxy (port or de-scope) → C# control-plane REST → C# tunnel host REST → optional cover/UI depth. **No C# MCP.**
