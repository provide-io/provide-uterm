# HANDOFF — Cross-language parity (Python / Go / C#)

## Status (2026-07-19)

Cross-language behavioral contract is **load-bearing** with shared goldens and
tri-language consumers. Multi-backend Playwright curated suite green for
`UTERM_TEST_BACKEND=python|go|csharp`. Dirty mid-flight Playwright WIP was
**restored** (not finished as-is) and replaced with a clean curated suite.

## What shipped

### Contract + goldens
- `spec/behavior.json` v1.1 — ops: `input_inject`, `hijack_step`,
  `hijack_release`, `hijack_acquire`; role ranks; hello defaults per language;
  stable `forbidden_*` error strings.
- `spec/behavior_vectors.json` — 48 parametrized policy cases + hello defaults.
- `scripts/generate_behavior_vectors.py` (also via `generate_parity_tests.py`)
  regenerates and copies vectors to Go/C#/Python test trees.

### Policy engines (same semantics)
| Language | Path |
|----------|------|
| Python | `packages/provide-uterm/src/provide/uterm/bridge/policy.py` |
| Go | `packages/provide-uterm-go/policy` (+ `vnc` type aliases) |
| C# | `packages/provide-uterm-csharp/src/Provide.Uterm/Policy/StrictPolicyEngine.cs` |

Tests load the **same** golden vectors (not placeholder asserts).

### Hello capabilities
- Python: already had `mcp_supported` / `vnc_supported` on `HelloFrame`.
- Go: fields on `HelloFrame`; `MakeHelloFrameWithDefaults()` → mcp=true, vnc=true.
- C#: fields on `HelloFrame` + mapper; `MakeHelloFrame()` → mcp=false, vnc=true.
- Defaults documented in `spec/behavior.json` `hello_defaults` and
  `docs/protocol-matrix.md`.

### Multi-backend Playwright
- `packages/provide-uterm/tests/playwright/backend_server.py` — subprocess
  launcher; sets `UTERM_TEST_MODE=1` only on children.
- `test_multi_backend_parity.py` — curated suite (health/TCP, worker WS with
  bearer, `page.route` browser smoke).

### Mutation / property
- Go gremlins perimeter adds **`policy`** (100% cover, 4 killed, 0 survivors).
- Python mutmut `source_paths` includes `bridge/policy.py`.
- Hypothesis property tests on Python policy; Go `testing/quick` on hello
  capability JSON round-trip.
- **C# residual:** no Stryker/mutation gate in-tree; compensated with golden
  vector matrix + `[Theory]` cases. Documented residual.

## Proofs run (scratch logs)
Implementer scratch: goal harness `{SCRATCH}`:
- `phase0-git-status.log`, capability unit smoke
- `conformance-tri.log` — Py/Go/C# policy vectors
- `property-fuzz.log` — hypothesis
- `mutation-go.log` — Go mutation gate passed (policy included)
- `mutation-python.log` — policy.py mutmut
- `pw-python.log`, `pw-go.log`, `pw-csharp.log` — multi-backend curated suite
- `interop.log` — Go interop skipped when PyJWT missing in bare `uterm` path
  (dev residual; multi-backend python subprocess via `uv run` works)

## Verification commands
```bash
uv run python scripts/generate_behavior_vectors.py
uv run pytest packages/provide-uterm/tests/bridge/test_behavior_policy.py --no-cov
cd packages/provide-uterm-go && go test ./policy/ ./frames/ ./vnc/ && make mutation-gate
dotnet test packages/provide-uterm-csharp --filter FullyQualifiedName~StrictPolicyEngine
UTERM_TEST_BACKEND=python uv run pytest packages/provide-uterm/tests/playwright/test_multi_backend_parity.py -m playwright --no-cov
UTERM_TEST_BACKEND=go     uv run pytest packages/provide-uterm/tests/playwright/test_multi_backend_parity.py -m playwright --no-cov
UTERM_TEST_BACKEND=csharp uv run pytest packages/provide-uterm/tests/playwright/test_multi_backend_parity.py -m playwright --no-cov
```

## Residuals
1. C# mutation tooling not installed — property/vector coverage only.
2. Go live interop may skip if `uterm` entrypoint lacks PyJWT; use project
   `uv run` env for full interop.
3. Full root `make quality-gate` + whole-module C# cover floor should be run
   in CI; local session ran targeted gates + multi-backend suite.
4. API docs scripts (`generate_api_docs.*`) remain; C# xmldocmd still stubbed
   (non-goal residual).

## Next session (optional)
- Align C# worker WS path to `/ws/worker/{id}/term` for wire path parity.
- Install Stryker.NET for C# policy package if desired.
- Expand curated multi-backend suite to hijack acquire/release UI flows.

## Skeptic follow-up (2026-07-19 later)

- Production Go `buildHelloFrame` and C# browser hello now stamp
  `mcp_supported`/`vnc_supported` (unit + e2e + ServerIntegration proof).
- Live Go↔Python interop both directions PASS (see interop.log).
- Root `make quality-gate` all checks passed; C# quality-gate 97.00% cover.
- Multi-backend suite asserts hello capability wire parity with JWT auth
  (no soft-fail on ws_err).

- C# browser/worker WS paths aligned to `/ws/{browser|worker}/{id}/term` (Python/Go).
