# Plan: provide-uterm first-party VNC web console (terminal-parity)

## Goal kind
code-change

## One-liner
Ship a **first-party browser VNC console** (like `terminal.html` + xterm.js) that
connects through provide-uterm’s human VNC relay to a real RFB target — using the
existing `uterm-test-vnc` lab as the proof backend — with plain and TLS-capable
upstream dial, and durable screenshots/video of the **uterm** UI (not bare noVNC).

## End state (definition of done)
An operator can:

1. Start `uterm-test-vnc` (plain `:5900` and/or TLS `:5901`).
2. Start `uterm server` with a seeded `[[graphical_targets]]` RFB entry pointing at that lab.
3. Open a **provide-uterm** page (served from the frontend/SPA package, same product
   surface as terminal/hijack) — e.g. `/vnc.html` or equivalent session GUI route.
4. After auth + hijack acquire, the page connects to  
   `WS /worker/{worker_id}/hijack/{hijack_id}/gui/vnc?target_id=…` as **binary RFB**.
5. The page shows the remote desktop (lab Chromium on `https://example.com`) and
   accepts pointer/keyboard when inject policy allows.
6. A committed proof path (script + docker-marked e2e and/or Playwright) exercises
   this stack twice and captures `{SCRATCH}` evidence including at least one
   screenshot of the **uterm** VNC page (and video if tools allow).

## Acceptance criteria
1. **Frontend VNC console exists in-repo** under `packages/provide-uterm-frontend/`
   (HTML + TS + CSS), patterned after `terminal.html` / `hijack.html`, using a real
   RFB client library (prefer `@novnc/novnc` or vendored equivalent — not a
   reimplementation of RFB). It opens a **binary** WebSocket to the human VNC
   path and paints the framebuffer into a canvas.
2. **Python server dials RFB upstreams** for `protocol = "rfb"` graphical targets
   so `/gui/vnc` no longer always closes with 1013 when a valid target is
   configured. Dial must support:
   - **plain** TCP RFB (lab `:5900` / Security None or VNC Auth)
   - **TLS** RFB (lab `:5901` / x11vnc `-ssl`, with configurable cert verify
     skip for lab self-signed — fail-closed verify by default outside lab)
3. **Config path**: `[[graphical_targets]]` (and/or demo TOML under `docker/` /
   `scripts/`) can declare the lab host:port, protocol, optional TLS flags; server
   seeds the registry at boot (existing `seed_graphical_targets`).
4. **Authz path remains real**: unauthenticated / non-operator / non-owner hijack
   still denied; inject still fail-closed via existing RFB filter + `can_inject`.
5. **Demo/proof path** (script or docker-marked test) starts lab + server, acquires
   a hijack, opens the uterm VNC page (or drives the same WS + RFB client path the
   page uses), proves desktop is visible / RFB stream live, and that the lab demo
   URL is still in evidence. Runs **twice** with consistent observables under
   `{SCRATCH}`.
6. **Visual proof**: at least one committed or scratch screenshot of the
   **provide-uterm** VNC console showing the remote desktop (example.com or
   clearly labeled lab desktop), not only an external noVNC window.
7. **Regression**: existing `test_ws_gui_vnc` / RFB filter tests still pass;
   structural tests assert frontend VNC assets exist without requiring Docker.

## Verification plan
1. `gating`: Unit/structural — frontend assets committed; RFB client import path
   real; Python dial helper unit tests for plain parse + TLS wrap (mock socket /
   local lab). Capture test log `{SCRATCH}/vnc-console-unit.log`.
2. `gating`: Live — start `uterm-test-vnc`; start server with lab
   `[[graphical_targets]]`; REST/hijack acquire as operator; open uterm VNC page
   or run proof script that uses the **same** WS URL the page uses. Observation:
   binary WS stays open; RFB ProtocolVersion exchange succeeds through the relay
   (not 1013). Logs: `{SCRATCH}/vnc-console-connect.log` and
   `{SCRATCH}/vnc-console-connect-2.log` (second run).
3. `gating`: Navigation/desktop evidence — screenshot of uterm VNC page
   (`{SCRATCH}/uterm-vnc-console.png`) and/or page title + canvas dimensions +
   status string naming connected target; lab-side `browser-nav.log` still shows
   demo URL. Optional video `{SCRATCH}/uterm-vnc-console.webm`.
4. `gating`: Encrypted path — at least one proof against lab **TLS port** (or
   config `tls=true` / `rfb+tls` endpoint form as designed). Observation: TLS
   handshake + RFB through relay succeeds; log `{SCRATCH}/vnc-console-tls.log`.
5. `gating`: Negative — without target / bad target_id / viewer-only principal,
   connect fails with expected close/HTTP; log snippet in
   `{SCRATCH}/vnc-console-denied.log`.
6. `evidence`: If Docker unavailable, record `{SCRATCH}/docker-unavailable.log`
   and do **not** claim live criteria; unit/structural may still pass.

## Non-goals
- Litevirt gRPC `ProxyVNC` path (already planned separately under
  `docs/superpowers/plans/2026-07-11-litevirt-vnc-integration.md`) — RFB TCP is enough.
- Full multi-tenant GUI product polish (recording GUI, multi-monitor, clipboard
  UX, file transfer, UltraVNC extensions).
- Replacing the terminal console; this is a **sibling** surface.
- Mandatory Go/C# SPA feature parity in the same PR stack (language servers may
  keep WS relay only; Python + frontend is the end-to-end product path for this
  goal). Stretch: document parity gap; optional follow-on goal for Go/C# dial.
- Publishing a public registry image or production mTLS PKI.
- Embedding websockify inside the server (browser talks **uterm** WS only).

## Assumed scope / existing building blocks
| Piece | Location | Reuse |
|-------|----------|--------|
| Terminal SPA pattern | `packages/provide-uterm-frontend/terminal.html`, `src/terminal-page.ts` | Page shell, Vite entry, auth/query params |
| Human VNC WS | `…/bridge/routes/ws_gui_vnc.py` | Authz + relay; add default `vnc_upstream_factory` |
| RFB filter / relay | `provide.uterm.vnc.rfb_filter`, `human_relay` | Unchanged semantics |
| Graphical targets | `graphical_targets.py`, `graphical_routes.py`, `config_schema.GraphicalTargetConfig` | Seed + `parse_rfb_endpoint` |
| VNC lab | `docker/vnc-lab/`, `scripts/prove_vnc_lab.py` | Proof backend (plain 5900 / TLS 5901) |
| Prior demos | `demo/vnc-lab/` (external noVNC proof) | Contrast: new proof must be **uterm** UI |

## Implementation approach
### A. Python upstream factory (server)
- Implement `open_rfb_upstream(target) -> (BinaryIO, BinaryIO)` (or async-safe
  socket pair) that:
  - Resolves `target_id` from `app.state.uterm_graphical_targets` / registry.
  - Parses endpoint via `parse_rfb_endpoint`.
  - Connects TCP; if TLS mode (config flag or `tls`/`ssl` in target config /
    `rfbs://` scheme if introduced), wrap with `ssl` (verify optional for lab).
  - Returns makefile streams for `run_human_relay_streams`.
- Wire factory on hub at app factory (`factory_impl.py`) so production path
  works without test monkeypatch.
- Keep 1013 when target missing/disabled/dial fails.

### B. Frontend VNC console
- Add `vnc.html` + `src/vnc-page.ts` (+ CSS) mirroring terminal self-assemble:
  query params for `worker_id`, `hijack_id`, `target_id`, auth headers/token as
  terminal/hijack already do.
- Use noVNC `RFB` class against `ws(s)://…/worker/…/hijack/…/gui/vnc?target_id=…`
  with `wsProtocols` / binary type appropriate for the library.
- Status chip: Connecting / Connected / Denied / Upstream unavailable.
- Input: respect view-only if lease/role is viewer (if page can know role;
  otherwise server filter already drops inject).

### C. Demo config + proof script
- Example TOML snippet: lab graphical target pointing at `host.docker.internal`
  or `127.0.0.1` published ports.
- `scripts/prove_uterm_vnc_console.py` (or extend demo recorder):
  1. ensure lab image/container
  2. start server with config
  3. create/acquire hijack session as operator
  4. Playwright/octowright open uterm VNC URL; assert canvas/status
  5. screenshot + second run
- Docker-marked pytest importing real proof helpers (no reimplementation).

### D. Docs
- Update `docker/vnc-lab/README.md` and `demo/vnc-lab/README.md` with “uterm
  console” section; short note in frontend README.
- Protocol matrix: mark human VNC browser path as shipped for Python frontend.

## Task checklist
- [ ] Design endpoint/TLS config shape for RFB graphical targets (plain + TLS);
      document in example TOML; unit-test parser/dial config.
- [ ] Implement Python `vnc_upstream_factory` + dial (plain); wire in app factory;
      e2e against lab `:5900` through `/gui/vnc` (no browser yet).
- [ ] Extend dial for TLS/lab cert mode; prove against lab `:5901`.
- [ ] Add frontend VNC page (HTML/TS/CSS + noVNC dependency) connecting binary WS
      to `/gui/vnc`; local unit/vitest smoke for URL/build.
- [ ] Wire SPA bake/serve path so server image or dev server hosts `vnc.html`
      like terminal/hijack.
- [ ] Add `scripts/prove_uterm_vnc_console.py` + docker-marked e2e: lab + server +
      page; two successive runs; `{SCRATCH}` logs + screenshot.
- [ ] Capture uterm-console screenshot (and optional video); place under
      `demo/vnc-lab/screenshots/` (git rules for media) + scratch.
- [ ] Docs + protocol-matrix / HANDOFF note; ensure quality-gate relevant tests green.

## Suggested phased PR stack (optional)
1. **PR1** — RFB upstream dial (plain) + config + tests (no UI).
2. **PR2** — TLS dial + lab dual-port proof via WS only.
3. **PR3** — Frontend VNC console page + bake.
4. **PR4** — Full prove script, screenshots, demo README.

## Risks / Contradictions
- **Binary WS + reverse proxies**: some proxies buffer text frames only; document
  need for binary WebSocket support (same class of issue as raw terminal bytes).
- **noVNC license/deps**: pin version; prefer npm dependency with SRI/vendoring
  consistent with frontend package policy.
- **SSRF**: RFB dial must respect existing egress/peer IP guards
  (`security` config); lab targets on loopback only in demos.
- **Self-signed TLS**: default verify-on; lab proof uses explicit
  `tls_insecure=true` (or equivalent) in demo config only.
- **Hijack requirement**: page cannot be “anonymous VNC”; flow needs worker +
  hijack lease like GUI attach — match existing REST GUI patterns in
  `test_rest_gui` / hijack APIs.
- Do **not** regress authz fail-closed behavior of `/gui/vnc`.

## Deviations
(append terse bullets only when execution diverges from this plan)

## How to launch this goal
Paste or reference this file when starting goal mode, for example:

```text
/goal Implement the plan in .provide/goals/uterm-vnc-web-console.md
```

or:

```text
/goal Ship a first-party provide-uterm VNC web console (terminal parity)
using the plan at .provide/goals/uterm-vnc-web-console.md as source of truth.
```

Copy of plan also at:
`docs/superpowers/plans/2026-07-21-uterm-vnc-web-console.md`
