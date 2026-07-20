# Goal — Test quality ratchet (priority 4 → 5 → 3 → 2)

**Status:** ready to execute  
**Head baseline:** `main` @ post-cover push (CI green; C# floor 97.9 / Go 97.5 / Python 100% scoped)  
**Order:** do phases **in sequence**; do not start phase N+1 until phase N is CI-green on `main`.

---

## Objective (paste for `/goal`)

```
Execute the provide-uterm test-quality ratchet in this fixed order until each phase is green on origin/main:

PHASE 4 — Multi-backend Playwright surface expansion
- Expand CI job multi-backend-playwright (matrix: python, go, csharp) beyond the current suite
  (hijack, deckmux e2e, resume, color_palette, multi_backend_parity, multi_backend_deckmux_resume).
- Add high-value modules that are already playwright-marked and multi-backend-capable (or make them so):
  chaos browser, inspect e2e, reconnect/spinner, browser control, terminal proxy — only where
  backends implement the surface (skip/xfail with clear markers, never silent empty pass).
- Requirements: all three backends green in CI; keep timeout budget (split job or filter -k if needed);
  no --cov on playwright; UTERM_TEST_MODE=1 + UTERM_MULTI_BACKEND=1 + UTERM_TEST_BACKEND matrix.
- Done when: CI multi-backend matrix green and HANDOFF lists new modules + any intentional backend skips.

PHASE 5 — Python server cov scope: include provide.uterm.cli (+ fastapi_utils if load-bearing)
- Expand packages/provide-uterm-server pyproject --cov= and [tool.coverage.run] source to include
  provide.uterm.cli (and fastapi_utils only if it remains in production import path).
- Drive line+branch back to fail_under=100: prefer unit tests; use site-local pragma: no cover only
  for true TUI/live-socket residuals (match existing gateway/share patterns). Remove or justify
  any leftover partial_branches regexes if they hide real branches.
- Done when: server-quality 3.11–3.14 green at 100% with expanded scope; no flaky 99% under random order.

PHASE 3 — Expand mutation perimeters (assertion quality, not vanity %)
- Python: add stable ~100%-covered pure modules to mutmut source_paths only after killed==100
  changed-only (and full chunk if required); document equivalents in mutation_equivalents.toml.
- Go: grow gremlins PERIMETER beyond sanitizer/colors/filters/lineeditor/redaction/channels/frames/policy
  one package at a time (e.g. ctrlmsg, ansi builders, auth, deckmux pure) with killed==100.
- C#: expand custom mutation_gate targets beyond current Policy/DeckMux/Sgr/Filters/Sanitizer/Redaction
  only where unit tests already pin behavior.
- Done when: all three language mutation CI jobs green with larger perimeter and allowlists only for
  documented equivalents.

PHASE 2 — Residual cover ratchet (C# / Go)
- C#: drive included residual (~223 lines: Embed cancel, UtermServer.Gui RFB catch, dual-OS FileIo)
  with harnesses; remeasure Ubuntu+Windows; raise COVER_THRESHOLD toward 98.5–99 only with ≥0.2pt
  dual-OS headroom (do not game by broadening EXCLUDE_SUBSTR without pure-helper extraction).
- Go: close remaining unit-testable server/manager/hub residual; remeasure; raise COVER_THRESHOLD
  toward 98.0 only with headroom after race-clean tests.
- Optional: dual-OS combined C# cover artifact so one floor works without Windows/Ubuntu delta fudge.
- Done when: new floors documented in Makefile+README and csharp-quality + csharp-quality-windows +
  go-quality green on main.

Global constraints for every phase:
- Work small, verify with real gates (make quality-gate / package cover / CI), commit without AI co-author
  trailers, push origin/main, leave tree clean.
- No live exploit tooling; no silent residual exclusion inflation.
- Update .provide/HANDOFF.md checklist when each phase completes.
```

---

## Phase checklist

### Phase 4 — Multi-backend surface
- [ ] Inventory playwright tests: multi-backend ready vs python-only
- [ ] Port or gate each candidate (route helpers, backend_server, skip matrix)
- [ ] Wire CI list / split if timeout
- [ ] CI green all backends
- [ ] HANDOFF update

### Phase 5 — Python CLI cov
- [ ] Map `cli/` + `fastapi_utils` coverage gaps
- [ ] Expand cov config
- [ ] Tests and/or justified pragmas
- [ ] server-quality 100% all Py versions
- [ ] HANDOFF update

### Phase 3 — Mutation perimeter
- [ ] Python: pick next source_paths; kill to 100
- [ ] Go: next pure package(s) in PERIMETER
- [ ] C#: next pure modules in mutation_gate
- [ ] All mutation CI jobs green
- [ ] HANDOFF update

### Phase 2 — Cover residual
- [ ] C# harnesses + dual-OS measure
- [ ] Go residual + race-clean
- [ ] Raise floors with headroom
- [ ] CI green; README/Makefile notes
- [ ] HANDOFF update

---

## Out of scope (unless product asks)
- Absolute 100% on live PTY/SSH/FxSsh/RFB accept races without a dedicated live harness job
- Expanding CF Worker cover past Pyodide/Web Crypto pragmas without real_cf infra
- Whole-repo mutmut (perimeter stays curated)
