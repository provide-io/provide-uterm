# uterm Remediation — Orchestration & Dispatch Plan

> **For the orchestrator:** This directory contains 8 self-contained remediation plans derived from the 2026-05-28 architecture review. They are partitioned by **file ownership** so they run as parallel lanes with zero merge conflicts. Read this file first, then dispatch per the wave plan below.

**Goal:** Resolve all confirmed bugs and security findings from the review, ordered for maximal parallelism and minimal rework.

**Source of findings:** the review covered the FastAPI server, Cloudflare worker, core library, client/MCP, platform/manager, and frontend. Four bugs (CB-1..CB-4) were verified against source by the reviewer.

---

## Dispatch model — why it is partitioned this way

The single most important optimization here is **conflict-free parallelism**. Each lane owns a *disjoint* slice of the tree:

| Lane | Plan file | Owns (exclusive write scope) | Critical-security? |
|------|-----------|------------------------------|--------------------|
| **A1 — Cloudflare** | `A1-cloudflare.md` | `packages/provide-uterm-cloudflare/**` | **YES** (CB-3) |
| **A2 — Client/MCP** | `A2-client-mcp.md` | `packages/provide-uterm-client/**` | **YES** |
| **A3 — Platform/Manager** | `A3-platform-manager.md` | `packages/provide-uterm-platform/**` | **YES** (CB-4) |
| **A4 — Server/Hub** | `A4-server-hub.md` | `packages/provide-uterm-server/**` | no (CB-2 + perf) |
| **A5 — Core** | `A5-core.md` | `packages/provide-uterm/**` | no (CB-1 + correctness) |
| **A6 — Infra/Docs/CI** | `A6-infra-docs.md` | root files only: `/CLAUDE.md`, `/pyproject.toml`, `/uv.lock`, `/.github/**`, `/ci/**`, `/.ci/**`, `/.pre-commit-config.yaml`, `/MUTATION_PATTERNS.md` | no |
| **A7 — Frontend** | `A7-frontend.md` | `packages/provide-uterm-frontend/**` | no (1 small fix) |
| **B1 — Parity conformance** | `B1-conformance.md` | new `tests/conformance/**` | depends on A1+A4 |

**Ownership rule (non-negotiable):** a lane edits ONLY files under its scope, *including that package's own `pyproject.toml`*. The root `pyproject.toml` is owned by **A6 alone**. No two lanes touch the same file. If a lane discovers it needs a change outside its scope, it records the request in its plan's "Cross-lane requests" section and does **not** make the edit — the orchestrator routes it.

---

## Wave plan

```
Wave A (all parallel — dispatch together, ideally each in its own git worktree):
   A1 Cloudflare ─┐
   A2 Client     ─┤
   A3 Platform   ─┼─ no inter-lane file overlap → run concurrently
   A4 Server     ─┤
   A5 Core       ─┤
   A6 Infra      ─┤
   A7 Frontend   ─┘

Wave B (after A1 + A4 land):
   B1 Parity conformance suite   (encodes the corrected FastAPI↔CF behavior)

Wave C (orchestrator, after everything merges):
   Full integration gate (see "Final integration gate" below)
```

**Priority within Wave A:** A1, A2, A3 carry the critical-severity security fixes — dispatch them first / give them the strongest model. A4, A5, A6, A7 can follow immediately in parallel; they are lower-risk.

**Why B1 is not in Wave A:** the conformance suite asserts the *corrected* parity between the FastAPI hub and the CF Durable Object. Writing it before A1/A4 land would pin the buggy behavior. Dispatch B1 only after A1 and A4 are merged and green.

---

## Shared spec — JWT algorithm-confusion guard (used by A1 and A4)

This finding spans two packages. To avoid a cross-lane dependency, **A1 and A4 each implement it in their own package**, but the logic MUST be identical. Both implement this guard at config-load / validation time:

> **Rule:** Reject any auth configuration that lists an HMAC algorithm (`HS256`/`HS384`/`HS512`) *together with* an asymmetric algorithm (`RS*`/`ES*`/`PS*`) **or** together with a configured PEM public key / JWKS URL. A config that has a PEM/JWKS key present must contain only asymmetric algorithms.

Rationale: if both `HS256` and an RSA public key are accepted, an attacker forges an `HS256` token using the public key bytes as the HMAC secret. The guard fires at startup (loud `ValueError`), never per-request.

A4 owns the server copy (`server/.../auth.py` or its config validator); A1 owns the CF copy (`cloudflare/.../config.py`). Each plan restates the rule so they can be read independently.

---

## Global constraints (every lane MUST obey)

These come from `CLAUDE.md` (project + user global) and the existing CI gates. Each lane plan references this section.

1. **TDD, red→green.** Before editing any production file, the lane MUST read the surrounding code, then write a failing test that pins the corrected behavior, confirm it fails, implement, confirm it passes. Exact `pytest` invocations are in each plan.
2. **100% branch+line coverage** is enforced on the measured perimeter (`--cov-fail-under=100`). Any new prod branch needs a covering test. Do not add `# pragma: no cover` to dodge this — if you think a branch is unreachable, prove it in the test or raise it as a cross-lane request.
3. **100% mutation kill** on the curated perimeter (auth, token hashing, intercept denylist, lease state machine, frame schemas, hub services). Lanes A1/A4/A5 touch perimeter files and MUST run `uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100` before declaring done.
4. **SPDX headers** on every new file (tests and scripts included):
   ```
   #
   # SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
   # SPDX-License-Identifier: AGPL-3.0-or-later
   #
   ```
5. **No hardcoded URLs/ports.** New defaults go in the package's existing defaults/config module or at the top of the file. If a needed port is already in use, STOP and ask.
6. **Logging:** use `pout`/`perr` (or the package's logger) — never `print`. Use `Path.cwd()`, never `os.getcwd()`.
7. **Commits:** one logical unit per commit; commit each finding separately (the plans are structured this way). Never batch unrelated fixes into one commit. Commit messages must NOT mention AI/Claude. Use conventional-commit prefixes (`fix:`, `feat:`, `test:`, `refactor:`, `docs:`, `ci:`).
8. **mypy strict + ruff** must pass on touched files: `uv run ruff check --fix && uv run ruff format && uv run mypy <touched package src>`.
9. **No git rollbacks** (changes auto-commit). If a step goes wrong, fix forward.
10. **Frame schemas:** if any lane changes `bridge/schemas.py`, it MUST run `uv run python scripts/codegen_frames.py` and commit `schemas.py` + `frames.schema.json` + `frames.ts` together (A5 only — schemas live in core).

---

## Per-lane verification commands

| Lane | Test command |
|------|--------------|
| A1 Cloudflare | `uv run pytest packages/provide-uterm-cloudflare/tests/ -q` |
| A2 Client | `uv run pytest packages/provide-uterm-client/tests/ -q` |
| A3 Platform | `uv run pytest packages/provide-uterm-platform/tests/ -q` |
| A4 Server | `uv run pytest packages/provide-uterm-server/tests/ -q` |
| A5 Core | `uv run pytest packages/provide-uterm/tests/ -q` |
| A6 Infra | `uv run python scripts/codegen_frames.py --check && uv run pip-audit` (+ workflow lint per plan) |
| A7 Frontend | `npm run typecheck:frontend && npm run lint:frontend && npm test -w packages/provide-uterm-frontend` |
| B1 Conformance | `uv run pytest tests/conformance/ -q` |

---

## Final integration gate (Wave C — orchestrator runs after all merges)

```bash
# 1. Every package's own suite + coverage (the Python side of CI)
uv run python scripts/run_all_tests.py

# 2. Full quality gate
uv run python scripts/run_pytest_gate.py -q

# 3. Mutation gate on everything touched on the branch
uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100

# 4. Frontend
npm ci && npm run build:frontend && npm run typecheck:frontend && npm run lint:frontend

# 5. Frame drift + CF vendor tree
uv run python scripts/codegen_frames.py --check
.ci/check_cf_vendor_tree.sh
```
All five must pass. If mutation survivors appear on a perimeter file, the owning lane fixes them before the branch is considered done.

---

## Findings → lane map (traceability)

| ID | Finding | Sev | Lane |
|----|---------|-----|------|
| CB-3 | CF `dev`/`none` = internet-facing admin bypass | 🔴 Critical | A1 |
| CF-svc | CF service-token auto-admin too loose (empty `sub`) | 🟡 Med | A1 |
| CF-lease | CF monotonic-clock lease persistence | 🔴 High | A1 |
| ALG (CF half) | JWT alg-confusion guard | 🟡 Med | A1 |
| MCP-send | `hijack_send` feeds unsanitized bytes to terminal | 🟠 High | A2 |
| MCP-host | `session_create` `host` unvalidated (SSRF) | 🟡 Med | A2 |
| MCP-redos | `expect_regex`/`pattern` ReDoS | 🟡 Med | A2 |
| CB-4 | Manager spawn sandbox opt-in (path traversal) | 🟠 High | A3 |
| PLAT-hmac | Webhook spawn-policy secret unused (no HMAC) | 🟠 High | A3 |
| PLAT-cors | Manager CORS `*` + credentials | 🟠 High | A3 |
| PLAT-fork | PTY fork child no catch-all before `_exit` | 🟡 Med | A3 |
| PLAT-cap | Unbounded `CaptureSocket._queue` | 🟢 Low | A3 |
| PLAT-reg | Manager auto-create unbounded by `max_agents` | 🟡 Med | A3 |
| CB-2 | `_paused_browsers` dead-socket leak | 🟠 Med | A4 |
| SRV-rl | Rate limiter FIFO-not-LRU, evict-then-recreate | 🟡 Med | A4 |
| SRV-bcast | Per-browser per-frame policy ctx + lock | 🟢 Low | A4 |
| SRV-share | Tunnel share-operator global `admin` scope | 🟢 Low | A4 |
| SRV-cookie | Cookie `secure` from spoofable `x-forwarded-proto` | 🟢 Low | A4 |
| ALG (srv half) | JWT alg-confusion guard | 🟡 Med | A4 |
| CB-1 | Emulator resize args swapped | 🔴 High | A5 |
| CORE-det | Detector pattern mutation not atomic | 🟡 Med | A5 |
| CORE-iso | memory vs sqlite transaction isolation parity | 🟡 Med | A5 |
| CORE-ord | `list_pending` ordering divergence | 🟢 Low | A5 |
| CORE-id | deckmux `id(ws)` identity collision | 🟢 Low | A5 |
| CORE-cov | coverage perimeter excludes `auth.py`/`control/` | 🟡 Med | A5 |
| INF-tel | `provide-telemetry` editable-sibling discrepancy | 🟠 High | A6 |
| INF-ci | inline-script policy violations in 3 workflows | 🟠 High | A6 |
| INF-doc | `MUTATION_PATTERNS.md` missing; CLAUDE.md drift | 🟡 Med | A6 |
| INF-branch | `partial_branches` regex escape-hatches | 🟡 Med | A6 (core copy → A5) |
| INF-ty | global `ty` `invalid-argument-type` ignore | 🟡 Med | A6 |
| FE-sel | deckmux `querySelector` userId interpolation | 🟢 Low | A7 |
| PARITY | FastAPI↔CF conformance suite | — | B1 |

> Note: `partial_branches` lives in **two** pyprojects. The root copy is A6; the `packages/provide-uterm/pyproject.toml` copy is A5 (owns its own package file).
