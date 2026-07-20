# HANDOFF — Cross-language parity (Python / Go / C#)

## Status (2026-07-19)

Cross-language behavioral contract is load-bearing. **Full heavy Playwright
hijack suite is green 20/20 on python, go, and csharp** under multi-backend
subprocess mode, with per-test screen recordings.

## What shipped (this session — heavy suite)

### Multi-backend heavy hijack
- `hijack_server` dual mode (`conftest_part1.py`):
  - default python → in-process TermHub (historic CI)
  - `UTERM_TEST_BACKEND=go|csharp` or `UTERM_MULTI_BACKEND=1` → real subprocess
    via `backend_server.py` + page.route HTML/UI (`ui_routes.py`)
- WorkerController sends `UTERM_TEST_WORKER_BEARER` when set
- Chromium Local Network Access flags for loopback WS
- `_navigate` installs multi-backend routes for second browser contexts

### C# production hijack WS (not test special-cases)
- Real `BroadcastHijackStateAsync` fan-out to browsers
- Wire field **`hijacked`** (was wrong `is_hijacked` — frontend dropped frames)
- Browser handshake: accurate hello from registry state + immediate `hijack_state`
- Heartbeat touch + `heartbeat_ack` when dashboard owner
- `worker_connected` / `worker_disconnected` browser fan-out
- Pause/resume/step control to worker on acquire/release/step

### Python TEST_MODE alignment
- `UTERM_TEST_MODE=1`: browser WS mint admin principal in
  `_require_authenticated` (factory_impl) — matches Go/C# admin without JWT
- Role forced admin after accept in `ws_browser_term`

## Proof matrix (heavy)

| Backend | Suite | Result | Artifacts |
|---------|-------|--------|-----------|
| python | `test_hijack.py` ×20 | **20 passed** ~20s | 20 webm + 20 png under `screenshots/backend-proof/heavy/python/` |
| go | same | **20 passed** ~19s | `…/heavy/go/` |
| csharp | same | **20 passed** ~29s | `…/heavy/csharp/` |

```bash
for be in python go csharp; do
  UTERM_TEST_BACKEND=$be UTERM_TEST_MODE=1 UTERM_MULTI_BACKEND=1 \
    uv run pytest packages/provide-uterm/tests/playwright/test_hijack.py \
      -m playwright --no-cov --video=on --screenshot=on \
      --output=packages/provide-uterm/tests/playwright/screenshots/backend-proof/heavy/$be
done
```

See `screenshots/backend-proof/heavy/summary.json`.

## Earlier parity (still load-bearing)
- `spec/behavior.json` + `behavior_vectors.json` + policy engines (Py/Go/C#)
- Hello `mcp_supported` / `vnc_supported` on production builders
- Canonical WS paths `/ws/{browser|worker}/{id}/term` (C# aligned)
- Curated `test_multi_backend_parity.py`

## Residuals
1. C# mutation tooling not installed — vector/property coverage only.
2. Deckmux/resume/color heavy multi-backend expansion not in this suite
   (hijack is the full heavy surface that shared hub APIs support today).
3. Full root `make quality-gate` should still run in CI before release.
4. In-process python path (no MULTI_BACKEND) remains for normal PT CI speed.

## DeckMux/resume multi-backend (2026-07-19 session 2)

### Production wiring
- Python `create_server_app`: default hub is DeckMuxMixin + TermHub
- Go CLI: `InMemoryResumeStore` on TermHub; browser `resume` handler reissues hello
- C#: resume tokens + presence_sync/update/leave on browser WS

### Suite
`test_multi_backend_deckmux_resume.py` — 6 tests × python/go/csharp (all green)

### quality-gate
`make quality-gate` — all checks passed after this work.

### Still residual
- Full Python Playwright deckmux HTML suite (`test_deckmux_e2e_*`) still uses
  in-process DeckMuxTermHub + `/deckmux-broadcast` helper (not multi-backend).
- Full `test_resume.py` still in-process with InMemoryResumeStore fixture.
- C# DeckMux is a production-path subset (not full pin/control-transfer port).
- C# mutation tooling still absent.

## All-four residuals closed (2026-07-20)

1. **Full deckmux HTML e2e multi-backend** — `test_deckmux_e2e_part1/2` dual-mode
   fixtures + `control_request` production path (no `/deckmux-broadcast` for xfer).
   **29/29** deckmux+resume+hijack on python/go/csharp with `UTERM_MULTI_BACKEND=1`.
2. **Full `test_resume.py` multi-backend** — dual-mode `resume_server`; 4/4 ×3.
3. **C# DeckMux pin/control** — `DeckMux/PresenceService.cs` with presence store,
   pin/scroll updates, control_request grant/release, leave fan-out.
4. **C# mutation gate** — `packages/provide-uterm-csharp/ci/mutation_gate.py` +
   `make mutation-gate` (Policy + DeckMux perimeter, 6 mutants killed/equiv).

Also: Go TEST_MODE deck disconnect leave; Go `mustInt(json.Number)` for WS
presence_update; Python TEST_MODE connection-scoped DeckMux principal.

## CI matrix + color multi-backend + C# mutation expand (2026-07-20)

### CI (`.github/workflows/ci.yml`)
- `multi-backend-playwright` matrix: python|go|csharp — hijack+deckmux+resume+static color
- `csharp-mutation-gate` job: `make -C packages/provide-uterm-csharp mutation-gate`

### Color multi-backend
- Dual-mode `color_server`; `color-test` HTML via `ui_routes`
- C# worker term/snapshot fan-out to browsers (required for ANSI pipeline)
- Static palettes 5/5 ×3 backends green

### C# mutation perimeter
- Policy, DeckMux, Colors/Sgr, Filters, Sanitizer, Redaction
- 13 mutants: 12 killed + 1 documented equivalent
