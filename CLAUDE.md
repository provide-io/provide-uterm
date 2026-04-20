# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**provide-terminal** is a terminal session platform that creates, transports, secures, shares, records, replays, and arbitrates terminal sessions across browsers, WebSockets, telnet, SSH, local PTYs, and remote workers. It's built around xterm.js as the screen, with provide-terminal managing the entire ecosystem.

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
uv run pytest packages/provide-terminal/tests/bridge/test_hub.py::test_name -vv

# Run tests for a single package
uv run pytest packages/provide-terminal-client/tests/ai/ -v

# Full quality gate (ruff, mypy, pytest, xenon, vulture)
uv run python scripts/run_pytest_gate.py -q

# Mutation testing (validates test quality, min score 100%)
uv run python scripts/run_mutation_gate.py --changed-only

# Linting & formatting
uv run ruff check --fix
uv run ruff format

# Type checking
uv run mypy packages/provide-terminal/src/
uv run ty check packages/provide-terminal/src/

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
| `provide-terminal` | Core library: ansi, screen, emulator, protocols, detection, deckmux, shell, render, replay |
| `provide-terminal-server` | Server stack: bridge hub, FastAPI server, CLI (`uterm`, `uterm-server`), tunnel, gateway |
| `provide-terminal-client` | Consumer libraries: HTTP/WS client, transports (telnet/SSH/WS), AI/MCP (`uterm-mcp`) |
| `provide-terminal-platform` | Platform targets: PTY connector, PAM, LD_PRELOAD capture, fleet manager (`uterm-manager`) |
| `provide-terminal-cloudflare` | CF Worker + Durable Object adapter |
| `provide-terminal-frontend` | Browser UI (vanilla TypeScript, xterm.js) |
| `provide-terminal-app` | App shell |

**Three-Layer Bridge System** (core architecture):
1. **HijackableMixin** — Worker-side mixin for hijackability at checkpoints
2. **TermHub** (`bridge/hub/`) — Server-side registry managing leases, roles, presence, I/O routing
3. **TermBridge** (`bridge/worker_link.py`) — Worker-side WebSocket client connecting to hub

**Control Channel**: JSON control frames (snapshots, hijack state, presence, analysis) mixed inline with raw terminal bytes in the same WebSocket stream.

**Lazy-loading module interface**: `provide/terminal/__init__.py` uses `__getattr__` to defer imports, avoiding hard dependencies.

## Testing

- **100% branch+line coverage** enforced (`--cov-fail-under=100`)
- **Mutation testing** enforced at 100% kill rate — see `MUTATION_PATTERNS.md` for patterns
- `asyncio_mode = "auto"` — async tests don't need `@pytest.mark.asyncio`
- Test markers: `playwright`, `mutant`, `memray`, `slow`, `e2e`, `real_cf`
- Default testpaths: `packages/provide-terminal/tests`, `packages/provide-terminal-cloudflare/tests`
- Root `conftest.py` handles mutmut source path manipulation — don't modify unless you understand mutation testing setup

## Pre-commit Hooks

Runs on commit: ruff (format+lint), mypy (strict), ty, bandit (security), biome (TS), vitest (frontend), reuse (SPDX headers), codespell. All new files need SPDX headers.

## Key Conventions

- Python >=3.11, line length 120, ruff lint rules: E/W/F/I/N/UP/B/C4/SIM/TCH/PTH/DTZ/S/ARG/RUF and more
- mypy strict mode enabled
- External dependency: `provide-telemetry` (sibling repo at `../provide-telemetry`, editable install)
- Config files: TOML-based server config (see `docker/server.toml`, `scripts/uterm-server.example.toml`)
- Auth modes: `dev` (no auth, local only) and `jwt` (production)
