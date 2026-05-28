# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**provide-uterm** is a terminal session platform that creates, transports, secures, shares, records, replays, and arbitrates terminal sessions across browsers, WebSockets, telnet, SSH, local PTYs, and remote workers. It's built around xterm.js as the screen, with provide-uterm managing the entire ecosystem.

Key capabilities: session control (hijack leasing with viewer/operator/admin roles), pluggable connectors, tunnel sharing, HTTP inspection/interception, collaborative presence (DeckMux), AI/MCP integration (21 tools), and agent fleet management.

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

# Full quality gate (ruff, mypy, pytest, xenon, vulture)
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

**Monorepo** using uv workspace (Python) + npm workspaces (TypeScript). 7 packages under `packages/`:

| Package | Role |
|---------|------|
| `provide-uterm` | Core library: ansi, screen, emulator, protocols, detection, deckmux, shell, render, replay |
| `provide-uterm-server` | Server stack: bridge hub, FastAPI server, CLI (`uterm`, `uterm-server`), tunnel, gateway |
| `provide-uterm-client` | Consumer libraries: HTTP/WS client, transports (telnet/SSH/WS), AI/MCP (`uterm-mcp`) |
| `provide-uterm-platform` | Platform targets: PTY connector, PAM, LD_PRELOAD capture, External Management Tier (`uterm-manager`) |
| `provide-uterm-cloudflare` | CF Worker + Durable Object adapter |
| `provide-uterm-frontend` | Browser UI (vanilla TypeScript, xterm.js) |
| `provide-uterm-app` | App shell |

**Three-Layer Bridge System** (core architecture):
1. **HijackableMixin** — Worker-side mixin for hijackability at checkpoints
2. **TermHub** (`bridge/hub/`) — Server-side registry managing leases, roles, presence, I/O routing
3. **TermBridge** (`bridge/worker_link.py`) — Worker-side WebSocket client connecting to hub

**Control Channel**: JSON control frames (snapshots, hijack state, presence, analysis) mixed inline with raw terminal bytes in the same WebSocket stream.

**Lazy-loading module interface**: `provide/uterm/__init__.py` uses `__getattr__` to defer imports, avoiding hard dependencies.

### Hub services

As of refactor #16 Phase 7, `TermHub` (`bridge/hub/core.py`) has zero mixin parents and composes nine service classes — `registry` (`WorkerRegistry`), `limiter` (`RateLimiter`), `approval_store` (`InMemoryApprovalStore`), `lease` (`HijackLeaseManager`), `router` (`MessageRouter`), `connection_mgr` (`ConnectionManager`), `presence_mgr` (`PresenceManager`), `state` (`StateStore`), `polling` (`PollingCoordinator`). Every legacy `hub.<method>(...)` call site still works: methods live directly on `TermHub` and forward to the appropriate service.

The full service map (with one-line descriptions of each) is in the docstring of `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/__init__.py`. New code should prefer `hub.<service>.<method>(...)` rather than the legacy facade methods.

## Testing

- **100% branch+line coverage** enforced (`--cov-fail-under=100`)
- **Mutation testing** enforced at 100% kill rate on a curated `paths_to_mutate` perimeter (security-critical surfaces + refactor #16 hub services + frame schemas). CI runs `scripts/run_mutation_gate.py --changed-only`, so the gate fires only on the perimeter files actually touched by a PR. See `MUTATION_PATTERNS.md` for patterns and `[tool.mutmut]` in root `pyproject.toml` for the full path list.
- `asyncio_mode = "auto"` — async tests don't need `@pytest.mark.asyncio`
- Test markers: `playwright`, `mutant`, `memray`, `slow`, `e2e`, `real_cf`
- Default testpaths: `packages/provide-uterm/tests`, `packages/provide-uterm-cloudflare/tests`
- Root `conftest.py` handles mutmut source path manipulation — don't modify unless you understand mutation testing setup

## Pre-commit Hooks

Runs on commit: ruff (format+lint), mypy (strict), ty, bandit (security), biome (TS), vitest (frontend), reuse (SPDX headers), codespell. All new files need SPDX headers.

## Key Conventions

- Python >=3.11, line length 120, ruff lint rules: E/W/F/I/N/UP/B/C4/SIM/TCH/PTH/DTZ/S/ARG/RUF and more
- mypy strict mode enabled
- External dependency: `provide-telemetry` (sibling repo at `../provide-telemetry`, editable install)
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
