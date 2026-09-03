# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**provide-uterm** is a terminal session platform that creates, transports, secures, shares, records, replays, and arbitrates terminal sessions across browsers, WebSockets, telnet, SSH, local PTYs, and remote workers. It's built around xterm.js as the screen, with provide-uterm managing the entire ecosystem.

Key capabilities: session control (hijack leasing with viewer/operator/admin roles), pluggable connectors, tunnel sharing, HTTP inspection/interception, collaborative presence (DeckMux), AI/MCP integration (28 tools), and agent fleet management.

## Build & Run Commands

```bash
# Install dependencies (or just `make sync`).
# --all-packages --all-extras is load-bearing, not belt-and-braces: plain
# `uv sync --group dev` resolves the ROOT project only and UNINSTALLS workspace
# members' own dependencies (psutil, provide-uterm-cloudflare). That leaves an
# env where test COLLECTION fails with ModuleNotFoundError, which reads as a
# broken dependency rather than a half-provisioned venv.
uv sync --all-packages --all-extras --group dev

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
- **Mutation testing** enforced changed-only at `killed==100` (`--min-mutation-score 100`) on a curated `source_paths` perimeter (security-critical surfaces + refactor #16 hub services + frame schemas + `manager/process_impl`). CI runs `scripts/run_mutation_gate.py --changed-only`; source changes select the touched perimeter files, while mutation support-file changes (`mutation_equivalents.toml`, mutmut config, gate scripts, or mutation test files) no longer silently pass with zero mutants and instead force an explicit perimeter decision. The perimeter is **fully enabled — nothing deferred — and green**: all 37 legs of the full-perimeter run passed on 2026-08-11 (run `31518962002`, commit `98c462f4`), the first green since 2026-06-14. `registry.py`, `routes/`, `webhooks.py`, `config_schema.py`, `connection.py`, `lease.py`, `detector.py`, and `manager/process_impl.py` were all driven to `killed==100` by 2026-06-02 (the 2026-06-01 "measured infeasible" audit was superseded once the mutmut `os.wait()` child-reaping crash was root-caused — see `docs/mutmut-survivors-triage.md` Wave 7). `routes/` then **regressed on 2026-07-23** and was repaired over 2026-08-09..10: `9bc4dd0c` moved the session handlers out of `@router.*` decorators into undecorated `*_capability_handlers` factories, and because **mutmut skips decorated functions**, ~2600 mutants went live at once behind tests whose line coverage was — and stayed — 100%. Watch for that mechanism on any de-decorating refactor; line coverage will not warn you. All seven `routes/` files are now at zero survivors. Do not read a red `mutation-full` run as "the standing failure" — and do not read a red *leg* as a mutation failure without opening its log, since a runner/TLS fault during checkout also reports as a failing leg. The `report` job writes the failing leg names to the run summary (`ci/report_mutation_full_failure.sh`), split into **drift** (the leg failed inside its `Mutation gate:` step — a real surviving mutant) and **not drift** (it failed in checkout/setup, which says nothing about the perimeter); it deliberately does **not** file a GitHub issue, and because it is gated on `needs.mutation-gate-full.result != 'success'` it does not run at all on a green run. **The cron is advisory by decision, not by omission** (2026-08-12): a weekly post-merge run has nothing to block, and making it a required check would only hold `main` red until someone fixed it — which is exactly the state that let it sit red for nine weeks unread. Instead `ci.yml`'s `quality` job runs `ci/report_perimeter_status.sh`, which echoes the last full-perimeter result into *your* push's run summary when it is red and prints nothing when it is green. That gives the scheduled run the audience it never had, without gating anything or adding write permissions. `manager/process.py`+`config.py` are NOT targets (0-mutant: a re-export shim + a Pydantic model). Genuinely-equivalent mutants are excused via the documented-equivalent allowlist `mutation_equivalents.toml` (a `timeout` is excusable ONLY for an allowlisted mutant) — e.g. `auth.py` is 100% after subtracting 11 such equivalents (94.02% raw). When a perimeter file is split for the 777-LOC limit, the extracted sibling module is added to `source_paths` so its mutants stay enforced. **Allowlist keys:** the three port gates (Go, C#, TS) key their allowlists on mutation *content* — `(file, mutator, code, original, replacement, occurrence)`, where `code` is the stripped source line — so an unrelated insertion above an entry no longer invalidates it, and one entry can no longer excuse several mutants at the same coordinates. They were `file:line:column:mutator` until 2026-08-12; that key broke sixteen C# entries at once on a 6-line fix, and repairing an allowlist is itself a support-file change that forces a full-perimeter run. Python's root `mutation_equivalents.toml` is the exception: it keys on mutmut's `…__mutmut_<n>` ids, which are function-scoped (an edit elsewhere in the file does not shift them) but still renumber when the mutant count inside that function changes. mutmut exposes no per-mutant content in its results output, so the same fix does not port over as-is. **A perimeter entry that is a shim reads exactly like coverage in the path list.** Wave 10 (2026-09-02, `docs/mutmut-survivors-triage.md`) found the perimeter listing `hub/router.py`, an 11-line re-export shim, while the router service's real code sat in `router_impl.py`/`router_broadcast.py`/`router_behavioral.py`/`router_redaction.py` — none of them listed — and `approvals.py` (the `approval_store` service) was absent outright. Four of those five are now closed and on the perimeter at `killed==100`: `router_redaction.py` (106 mutants), `router_behavioral.py` (112), `approvals.py` (105) and `router_broadcast.py` (507, of which 211 survived cold — the entire failure path, the 0/1-vs-many send split, and every line of `broadcast_hijack_state`'s second pass, all behind 100% line coverage). **`router_impl.py` (613 lines, ~976 mutants) is still unlisted and unenforced.** Land these one file at a time: a support-file change prints `FULL_PERIMETER_REQUIRED_MARKER` and returns 0, so each file can be measured, killed, added to `source_paths` and pushed green on its own rather than holding `main` red for a whole wave. Two traps met while doing it: `--paths` takes the `src/...` form that `source_paths` uses, and passing the repo-relative `packages/...` path instead silently misses `scoped_test_selection` and runs the wrong (broader) suite; and a kill-suite wired only into root `pyproject.toml` is dropped on a scoped run — `BRIDGE_HUB_MUTATION_TESTS` in `scripts/mutation_gate_config.py` is the list that changed-only runs actually consult. See `MUTATION_PATTERNS.md` for patterns and `[tool.mutmut]` in root `pyproject.toml` for the full path list + per-file obstacle notes.
- `asyncio_mode = "auto"` — async tests don't need `@pytest.mark.asyncio`
- Test markers: `playwright`, `mutant`, `memray`, `slow`, `e2e`, `real_cf`
- Default testpaths: `packages/provide-uterm/tests`, `packages/provide-uterm-cloudflare/tests`
- Root `conftest.py` handles mutmut source path manipulation — don't modify unless you understand mutation testing setup
- **Hypothesis profiles** live in `hypothesis_profiles.py` (repo root), activated from `conftest.py`, `packages/provide-uterm/conftest.py`, and `packages/provide-uterm-server/tests/conftest.py` — the three pytest rootdirs. Profiles: `dev` (50 examples, the default off-CI), `ci` (250, auto-selected when `$CI` is set), `deep` (1000, the nightly cron), `repro` (derandomized + no database, for triaging a suspected flake). Override with `HYPOTHESIS_PROFILE=<name>`. All profiles set `print_blob=True`, so any failure prints a pasteable `@reproduce_failure(...)`. The example database is pinned to `<repo>/.hypothesis/examples` (one corpus regardless of cwd) and cached across CI runs by the `quality` and `server-quality` jobs — restore/save rather than `actions/cache`, so a *failing* run's counterexample is persisted and replayed on retry. Never force `--hypothesis-seed` in CI: `core.run_engine` sets `database_key = None` whenever a global seed is forced, silently disabling the cached corpus. Profiles are a no-op inside mutmut's `mutants/` tree so mutant-induced failures never enter the corpus.

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

## Reporting External State

**A state claim is only true as of the moment it was read.** The gap between
reading and saying is where the wrong answers live.

Before asserting anything about mutable external state — CI run results, a
browser's current page, what is on PyPI, whether a workflow fired — either read
it in the *same* turn as the assertion, or write the timestamp into the sentence
("as of 17:06, no run had fired"). A bare present-tense claim from a read taken
several tool calls ago is how a stale observation gets reported as current fact.

`scripts/state.sh` prints one timestamped snapshot of the state that is easiest
to get wrong: git position, the latest push *and* scheduled CI runs with their
failing jobs, the latest release run and its publish jobs, and the versions on
both package indexes. Run it before reporting status rather than assembling the
answer from memory.

Three specific traps it encodes, each of which produced a confident wrong answer:

- **Scheduled runs execute jobs no push runs.** A green push says nothing about
  them, so they are listed separately.
- **The jobs API pages at 30.** A run with 49 jobs hides everything past the
  thirtieth, which is how a failing job went unseen for six days. Always
  `--paginate` with `per_page=100`.
- **PyPI's JSON API serves stale data for minutes after an upload** while the
  simple index is already correct. Read the simple index.

Absence of evidence is not evidence of absence, and it is usually latency: a
workflow that has not fired after four minutes may fire after fifteen. Say what
was observed and when, not what is.
