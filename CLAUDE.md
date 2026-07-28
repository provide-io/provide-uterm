# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**provide-uterm** is a terminal session platform that creates, transports, secures, shares, records, replays, and arbitrates terminal sessions across browsers, WebSockets, telnet, SSH, local PTYs, and remote workers. It's built around xterm.js as the screen, with provide-uterm managing the entire ecosystem.

Key capabilities: session control (hijack leasing with viewer/operator/admin roles), pluggable connectors, tunnel sharing, HTTP inspection/interception, collaborative presence (DeckMux), AI/MCP integration (28 tools), and agent fleet management.

## Build & Run Commands

```bash
# Install dependencies
uv sync --group dev

# Run the core + Cloudflare test suites (what root `pytest` covers — see
# [tool.pytest.ini_options].testpaths). 100% branch coverage enforced.
uv run pytest

# Run every workspace package's Python tests sequentially with its own
# coverage config (core, cloudflare, server, platform/manager, platform/pty).
# This covers the Python side of what CI runs. CI also runs npm vitest
# (npm-quality job) + CF python_modules vendor check (.ci/check_cf_vendor_tree.sh)
# — run those separately if you want full CI parity locally.
uv run python scripts/run_all_tests.py

# Run a single test
uv run pytest packages/provide-uterm/tests/bridge/test_hub.py::test_name -vv

# Run tests for a single package
uv run pytest packages/provide-uterm-client/tests/ai/ -v

# Static quality gate — the exact checks CI's `quality` job runs (max-LOC,
# SPDX headers, codegen-frames drift, event literals, ruff, mypy/ty, bandit,
# xenon, vulture, pip-audit, licenses, performance smoke, CF vendor tree,
# package artifacts). Run before pushing so CI-only failures surface locally.
# CI and this command share one source of truth: ci/quality_checks.sh.
make quality-gate            # == bash ci/quality_checks.sh

# Pytest worker-capping wrapper (caps -n workers at half the CPU count). This
# runs ONLY pytest — the lint/type/static checks above live in quality-gate.
uv run python scripts/run_pytest_gate.py -q

# Mutation testing (validates test quality, min score 100%)
uv run python scripts/run_mutation_gate.py --changed-only

# Linting & formatting
uv run ruff check --fix
uv run ruff format

# Type checking
uv run mypy packages/provide-uterm/src/
uv run ty check packages/provide-uterm/src/

# Frontend (TypeScript)
npm ci
npm run build:frontend
npm run typecheck:frontend
npm run lint:frontend

# Playwright browser tests
uv run pytest -m playwright

# Docker (both backends)
docker compose -f docker/docker-compose.yml up
# FastAPI: localhost:27780, CF Worker: localhost:27788
```

## Architecture

**Monorepo** using uv workspace (Python) + npm workspaces (TypeScript), plus standalone Go and C# ports. 11 packages under `packages/`:

| Package | Role |
|---------|------|
| `provide-uterm` | Core library: ansi, screen, emulator, protocols, detection, deckmux, shell, render, replay |
| `provide-uterm-server` | Server stack: bridge hub, FastAPI server, CLI (`uterm`, incl. the `uterm server` subcommand), tunnel, gateway |
| `provide-uterm-client` | Consumer libraries: HTTP/WS client, transports (telnet/SSH/WS), AI/MCP (`uterm-mcp`) |
| `provide-uterm-platform` | Platform targets: PTY connector, PAM, LD_PRELOAD capture, External Management Tier (`uterm-manager`) |
| `provide-uterm-cloudflare` | CF Worker + Durable Object adapter |
| `provide-uterm-annotation` | Annotation layer (100% coverage gate, own CI job) |
| `provide-uterm-frontend` | Browser UI (vanilla TypeScript, xterm.js) |
| `provide-uterm-app` | App shell |
| `provide-uterm-go` | Standalone Go port (module, own toolchain/CI — not part of the uv/npm workspaces) |
| `provide-uterm-csharp` | Standalone C# port (.NET 10, own quality gate) |
| `provide-uterm-ts` | TypeScript runtime port (Node >= 22, TS 7, npm workspace member) |

**Three-Layer Bridge System** (core architecture):
1. **HijackableMixin** — Worker-side mixin for hijackability at checkpoints
2. **TermHub** (`bridge/hub/`) — Server-side registry managing leases, roles, presence, I/O routing
3. **TermBridge** (`bridge/worker_link.py`) — Worker-side WebSocket client connecting to hub

**Control Channel**: JSON control frames (snapshots, hijack state, presence, analysis, inspect controls) are DLE/STX-framed and mixed inline with raw terminal bytes in the same WebSocket stream. Use the public control-frame helpers (`encode_control_frame`, client `send_frame`/`send_json`, frontend `encodeWsFrame`/`encodeControlFrame`) for every non-terminal WebSocket payload; CI runs `scripts/check_bare_json_ws_sends.py` to block obvious bare JSON sends on terminal/control paths.

**Module import strategy**: the core `provide/uterm/__init__.py` uses **eager** imports (it re-exports from `ansi`, `auth`, `colors`, `control_channel_*`, etc. at import time). The **lazy** `__getattr__`-based deferral lives in the *client* package (`packages/provide-uterm-client/src/provide/uterm/client/__init__.py`), which defers optional/heavy imports to avoid hard dependencies.

### Hub services

As of refactor #16 Phase 7, `TermHub` (`bridge/hub/core.py`) has zero mixin parents and composes nine service classes — `registry` (`WorkerRegistry`), `limiter` (`RateLimiter`), `approval_store` (`InMemoryApprovalStore`), `lease` (`HijackLeaseManager`), `router` (`MessageRouter`), `connection_mgr` (`ConnectionManager`), `presence_mgr` (`PresenceManager`), `state` (`StateStore`), `polling` (`PollingCoordinator`). Every legacy `hub.<method>(...)` call site still works: methods live directly on `TermHub` and forward to the appropriate service.

The full service map (with one-line descriptions of each) is in the docstring of `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/__init__.py`. New code should prefer `hub.<service>.<method>(...)` rather than the legacy facade methods.

## Testing

- **100% branch+line coverage** enforced (`--cov-fail-under=100`)
- **Mutation testing** enforced changed-only at `killed==100` (`--min-mutation-score 100`) on a curated `source_paths` perimeter (security-critical surfaces + refactor #16 hub services + frame schemas + `manager/process_impl`). CI runs `scripts/run_mutation_gate.py --changed-only`; source changes select the touched perimeter files, while mutation support-file changes (`mutation_equivalents.toml`, mutmut config, gate scripts, or mutation test files) no longer silently pass with zero mutants and instead force an explicit perimeter decision. The perimeter is now **fully enabled — nothing deferred**: `registry.py`, `routes/`, `webhooks.py`, `config_schema.py`, `connection.py`, `lease.py`, `detector.py`, and `manager/process_impl.py` were all driven to `killed==100` (the 2026-06-01 "measured infeasible" audit was superseded once the mutmut `os.wait()` child-reaping crash was root-caused — see `docs/mutmut-survivors-triage.md` Wave 7). `manager/process.py`+`config.py` are NOT targets (0-mutant: a re-export shim + a Pydantic model). Genuinely-equivalent mutants are excused via the documented-equivalent allowlist `mutation_equivalents.toml` (a `timeout` is excusable ONLY for an allowlisted mutant) — e.g. `auth.py` is 100% after subtracting 11 such equivalents (94.02% raw). When a perimeter file is split for the 777-LOC limit, the extracted sibling module is added to `source_paths` so its mutants stay enforced. See `MUTATION_PATTERNS.md` for patterns and `[tool.mutmut]` in root `pyproject.toml` for the full path list + per-file obstacle notes.
- `asyncio_mode = "auto"` — async tests don't need `@pytest.mark.asyncio`
- Test markers: `playwright`, `mutant`, `memray`, `slow`, `e2e`, `real_cf`
- Default testpaths: `packages/provide-uterm/tests`, `packages/provide-uterm-cloudflare/tests`
- Root `conftest.py` handles mutmut source path manipulation — don't modify unless you understand mutation testing setup

## Pre-commit Hooks

Runs on **every commit**: `codegen-frames` (Pydantic→TS frame-schema drift check), ruff (format+lint), reuse (SPDX headers), codespell, bandit (security), detect-secrets. All new files need SPDX headers.

Staged as `stages: [manual]` (NOT run on a normal commit — invoke with `pre-commit run --hook-stage manual`): mypy (strict), ty, mypy-platform, and the frontend hooks tsc/biome/vitest. mypy/ty are manual until accumulated type drift is cleaned up; the TS hooks are manual because they need a fresh `npm install` in `packages/provide-uterm-frontend/` first. See the header comment in `.pre-commit-config.yaml` for the rationale.

## Key Conventions

- Python >=3.11, line length 120, ruff lint rules: E/W/F/I/N/UP/B/C4/SIM/TCH/PTH/DTZ/S/ARG/RUF and more
- mypy strict mode enabled
- External dependency: `provide-telemetry` is resolved from **PyPI** (root floor `>=0.4.4`; the committed `uv.lock` pins it at the latest published release). There is **no** `[tool.uv.sources]` editable entry for it, so a local sibling checkout at `../provide-telemetry` is NOT picked up automatically. To develop against the sibling, add `provide-telemetry = { path = "../provide-telemetry", editable = true }` under `[tool.uv.sources]` and re-run `uv sync` locally — but do not commit that change or the resulting lock (the path is relative to the workspace root and breaks in git worktrees / CI).
- Config files: TOML-based server config (see `docker/server.toml`, `scripts/uterm-server.example.toml`)
- Auth modes: `dev_token` (local-only stub IdP that mints a JWT), `jwt` (production), `header` (proxy-stripped headers; requires loopback bind or `auth.trusted_proxy_ips` allowlist), `api_key`, `webhook` (delegated IDP; `auth.webhook_idp_on_failure` defaults to `deny`). Legacy server-side `dev`/`none` modes are removed.

## Frame Schemas (Python ↔ TypeScript)

The WebSocket wire-format frame definitions live in **one place**:
`packages/provide-uterm/src/provide/uterm/bridge/schemas.py`. These are
Pydantic v2 models behind a discriminated union (`AnyFrame`, key = `type`).

- **Python producers** (e.g. `make_*_frame()` builders in
  `provide-uterm-server/.../bridge/frames.py`) instantiate the model and
  call `.model_dump()` — wire bytes match the legacy hand-built dicts.
- **TypeScript consumers** import from
  `packages/provide-uterm-frontend/src/generated/frames.ts`, which is
  generated from the JSON Schema. Both `frames.ts` and
  `frames.schema.json` are committed and carry an AUTO-GENERATED banner.
  Hand-editing them is forbidden — the codegen check would fail.

To add a new frame type:
1. Add the model in `schemas.py` and include it in `AnyFrame`.
2. Run `uv run python scripts/codegen_frames.py`.
3. Commit `schemas.py`, `frames.schema.json`, and `frames.ts` together.

Pre-commit + CI run `scripts/codegen_frames.py --check` to catch drift.
