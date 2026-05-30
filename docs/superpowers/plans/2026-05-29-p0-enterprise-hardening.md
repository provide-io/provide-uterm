<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# P0 Enterprise-Hardening Remediations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven mechanically-clear, GA-blocking findings from the 2026-05-29 enterprise review (`docs/enterprise-hardening-review-2026-05-29.md`) — each a small, behavior-preserving, fully-testable change.

**Architecture:** Pure surgical fixes — no new subsystems, no architectural change. Every task is one cohesive change + its tests + one commit. Each is independently shippable and reversible. The codebase enforces **100% branch+line coverage** and **mutation testing** on a security perimeter, so every new branch needs a test that also *kills mutants* (test both sides of each new conditional).

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pytest (`asyncio_mode="auto"` — async tests need **no** `@pytest.mark.asyncio`), `respx` for HTTP mocking, `uv` for everything. Run a single test with `uv run pytest <path>::<name> -vv`.

**What this plan deliberately EXCLUDES** (they are real P0/P1 findings but need a design decision, not a mechanical edit — see "Design-first track" at the bottom; do **not** fake their code here):
- No-echo password-keystroke masking (there is currently **no** echo-state signal anywhere in the runtime — needs a source).
- Snapshot/analysis role-scoped output redaction (needs a role-scoped redactor design).
- Resume-token atomic single-use (changes the control-plane token-store protocol + both store impls).
- Connector/MCP runtime SSRF guard (needs DNS re-resolution + egress allowlist; this plan does the cheap *config-load scheme* half only).

---

## File Structure

| File | Change | Task |
|---|---|---|
| `packages/provide-uterm-server/src/provide/uterm/server/app/control_plane.py` | Correct `durable_state` for sqlite | 1 |
| `packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py` | Correct startup durability log | 1 |
| `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/ext.py` | `fail_open` param on behavioral gate (default closed) | 2 |
| `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/approvals.py` | Add atomic `claim()` | 3 |
| `packages/provide-uterm-server/src/provide/uterm/server/routes/approvals.py` | Claim-then-inject ordering | 3 |
| `packages/provide-uterm-platform/src/provide/uterm/pty/pam_listener.py` | `chmod 0o600` the notify socket | 4 |
| `packages/provide-uterm/src/provide/uterm/recording.py` | `chmod 0o600` recording files | 5 |
| `packages/provide-uterm/src/provide/uterm/session_logger.py` | `chmod 0o600` legacy-store files | 5 |
| `.github/workflows/ci.yml` | `pip_audit --path .` → `--local` | 6 |
| `packages/provide-uterm-server/src/provide/uterm/server/config_schema.py` | Reject cleartext `http://` outbound URLs | 7 |

Test files: `.../tests/server/test_durability_capabilities.py` (mod), `.../tests/bridge/test_governance_gate_failclosed.py` (new), `.../tests/bridge/test_approval_claim.py` (new), `.../tests/pty/test_pam_listener.py` (mod), `packages/provide-uterm/tests/test_recording_permissions.py` (new), `.../tests/server/test_outbound_url_scheme.py` (new).

---

## Task 1: Stop advertising approvals/leases as durable

**Why:** `/api/durability/capabilities` and the startup log claim `approvals` + `leases` survive restart in sqlite mode, but the factory only persists resume tokens; approvals/leases are in-memory. Operators architecting failover on this lose them silently. (Review finding G-high.)

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/app/control_plane.py:54-72`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py:152-157`
- Test: `packages/provide-uterm-server/tests/server/test_durability_capabilities.py`

- [ ] **Step 1: Write the failing test**

Append to `test_durability_capabilities.py`:

```python
from types import SimpleNamespace

from provide.uterm.server.app.control_plane import _build_durability_capabilities


def test_sqlite_durability_does_not_claim_approvals_or_leases() -> None:
    cfg = SimpleNamespace(control_plane=SimpleNamespace(backend="sqlite"))
    caps = _build_durability_capabilities(cfg)
    # Only what the factory actually persists may be advertised as durable.
    assert "resume_tokens" in caps.durable_state
    assert "control_plane_session_records" in caps.durable_state
    assert "approvals" not in caps.durable_state
    assert "leases" not in caps.durable_state
    # The unwired stores must be disclosed as process-local instead.
    assert "approvals" in caps.process_local_state
    assert "leases" in caps.process_local_state
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest packages/provide-uterm-server/tests/server/test_durability_capabilities.py::test_sqlite_durability_does_not_claim_approvals_or_leases -vv`
Expected: FAIL — `assert "approvals" not in caps.durable_state` (approvals is currently advertised durable).

- [ ] **Step 3: Fix `control_plane.py`**

Replace the sqlite `durable_state` block and `process_local_state` (lines 54-72):

```python
    if backend == "sqlite":
        durable_state = (
            "control_plane_session_records",
            "resume_tokens",
        )
    process_local_state = (
        "tunnel_tokens",
        "webhook_registrations",
        "fanout_groups",
        "approvals",
        "leases",
        "live_session_arbitration",
        "session_registry_runtime_state",
    )
    notes = (
        "SQLite mode persists only the resume-token and session-record stores wired into the factory.",
        "Approvals and hijack leases are in-memory and are LOST on restart.",
        "Tunnel tokens, webhook registrations, and fan-out groups also remain process-local.",
        "Run one active FastAPI control-plane instance, or use the durable backend for HA deployments.",
    )
```

- [ ] **Step 4: Fix the startup log in `factory_impl.py:152-157`**

```python
        logger.info(
            "standalone_server_durability=sqlite: shared control-plane stores (sessions, resume tokens) are "
            "persisted to %s. Approvals and hijack leases are in-memory and LOST on restart; tunnel tokens, "
            "webhook registrations, fan-out groups, and live runtime state also remain process-local; "
            "see /api/durability/capabilities.",
            config.control_plane.database_url,
        )
```

- [ ] **Step 5: Run the test + the existing durability tests**

Run: `uv run pytest packages/provide-uterm-server/tests/server/test_durability_capabilities.py -vv`
Expected: PASS (all). If a pre-existing test asserted the old 4-tuple, update it to the new contract (it was asserting a false guarantee).

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/app/control_plane.py \
        packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py \
        packages/provide-uterm-server/tests/server/test_durability_capabilities.py
git commit -m "fix(server): stop advertising in-memory approvals/leases as durable"
```

---

## Task 2: Make the behavioral-audit gate fail closed

**Why:** `WebhookBehavioralAuditGate.audit_connection` returns `allow` on non-200 **and** on any exception ("Default to allow on error"). A slow/unreachable governance webhook silently disables anomaly detection. Default to deny; keep an explicit `fail_open` opt-out for operators who prefer availability over the control. (Review finding F-medium; fail-open posture table.)

> **Trade-off to state in the PR:** with `fail_open=False`, a webhook outage will *close* anomalous-looking connections (`audit_all_browsers` acts on `deny`). That is the security-correct default for a gate; operators who would rather keep sessions up during an outage set `fail_open=True`.

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/ext.py:193-223`
- Test: `packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import httpx

from provide.uterm.server.bridge.hub.ext import (
    BehavioralThresholds,
    ConnectionHeuristics,
    PolicyContext,
    WebhookBehavioralAuditGate,
)

_H = ConnectionHeuristics(cps=1.0, jitter=0.0, timestamp=0.0)
_CTX = PolicyContext(worker_id="w1")
_T = BehavioralThresholds()


async def test_behavioral_gate_denies_when_webhook_unreachable() -> None:
    gate = WebhookBehavioralAuditGate(url="http://127.0.0.1:1/never", timeout_s=0.05)
    decision = await gate.audit_connection(_H, _CTX, _T)
    assert decision.action == "deny"


async def test_behavioral_gate_fail_open_opt_out_allows_on_error() -> None:
    gate = WebhookBehavioralAuditGate(url="http://127.0.0.1:1/never", timeout_s=0.05, fail_open=True)
    decision = await gate.audit_connection(_H, _CTX, _T)
    assert decision.action == "allow"


async def test_behavioral_gate_denies_on_non_200(respx_mock) -> None:
    respx_mock.post("https://gov.example/audit").mock(return_value=httpx.Response(500))
    gate = WebhookBehavioralAuditGate(url="https://gov.example/audit")
    decision = await gate.audit_connection(_H, _CTX, _T)
    assert decision.action == "deny"
```

- [ ] **Step 2: Run them and confirm failure**

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py -vv`
Expected: FAIL — current code returns `allow` on error, and `WebhookBehavioralAuditGate.__init__` has no `fail_open` kwarg (TypeError on the opt-out test).

- [ ] **Step 3: Add the `fail_open` flag and flip the default**

In `ext.py`, change `WebhookBehavioralAuditGate.__init__` (line ~196):

```python
    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0, *, fail_open: bool = False):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s
        self.fail_open = fail_open
```

And both error returns in `audit_connection` (lines 221 and 223):

```python
                if resp.status_code == 200:
                    return PolicyDecision(**resp.json())
                return PolicyDecision(action="allow" if self.fail_open else "deny")
        except Exception:
            return PolicyDecision(action="allow" if self.fail_open else "deny")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py -vv`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/ext.py \
        packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py
git commit -m "fix(server): behavioral-audit gate fails closed by default with fail_open opt-out"
```

> Follow-up (separate small PR, not this task): surface `fail_open` via `GovernanceConfig.behavioral_fail_open` and pass it through `factory_impl` when constructing the gate.

---

## Task 3: Close the approval resolve/reject TOCTOU

**Why:** `approve_command`/`reject_command` check `PENDING`, then `await resolve_approval` (which injects the held command at a yield point), then flip status. Two concurrent admin calls both pass the check → the command is injected **twice** (or approved+rejected). Inject only after winning an atomic claim. (Review finding D-high.)

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/approvals.py` (add `claim`)
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/routes/approvals.py:50-82`
- Test: `packages/provide-uterm-server/tests/bridge/test_approval_claim.py` (new)

- [ ] **Step 1: Write the failing test for the atomic primitive**

Create `packages/provide-uterm-server/tests/bridge/test_approval_claim.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

from provide.uterm.server.bridge.hub.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalStore,
)


def _pending(req_id: str = "r1") -> ApprovalRequest:
    return ApprovalRequest(
        id=req_id,
        worker_id="w1",
        submitter_id="s1",
        command="rm -rf /",
        status=ApprovalStatus.PENDING,
        created_at=0.0,
        expires_at=1e12,
    )


def test_claim_succeeds_exactly_once() -> None:
    store = InMemoryApprovalStore()
    store.add(_pending())
    assert store.claim("r1", ApprovalStatus.APPROVED) is True
    # A second claim (e.g. concurrent reject) must lose — status already moved.
    assert store.claim("r1", ApprovalStatus.REJECTED) is False
    assert store.get("r1").status == ApprovalStatus.APPROVED


def test_claim_missing_request_returns_false() -> None:
    store = InMemoryApprovalStore()
    assert store.claim("nope", ApprovalStatus.APPROVED) is False
```

- [ ] **Step 2: Run it and confirm failure**

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_approval_claim.py -vv`
Expected: FAIL — `InMemoryApprovalStore` has no `claim` attribute.

- [ ] **Step 3: Add the atomic `claim` to `approvals.py`**

Insert after `resolve` (after line 67):

```python
    def claim(self, request_id: str, status: ApprovalStatus) -> bool:
        """Atomically transition a PENDING request to *status*.

        Returns ``True`` only for the caller that performs the transition, so a
        held command is resolved — and therefore injected — exactly once even
        under concurrent approve/reject requests. Callers MUST inject the
        command only when this returns ``True``.
        """
        with self._lock:
            req = self._requests.get(request_id)
            if req is None or req.status != ApprovalStatus.PENDING:
                return False
            req.status = status
            return True
```

- [ ] **Step 4: Run the primitive test**

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_approval_claim.py -vv`
Expected: PASS (2 tests).

- [ ] **Step 5: Reorder the routes to claim-then-inject**

In `routes/approvals.py`, replace `approve_command`'s body (lines 58-65) so the atomic claim gates the injection:

```python
        if not hub._approval_store.claim(request_id, ApprovalStatus.APPROVED):
            raise HTTPException(status_code=400, detail="Approval request is not pending")
        await hub.resolve_approval(
            approval_req.worker_id, request_id, PolicyDecision(action="allow"), approval_req.command
        )
        return {"status": "approved"}
```

And `reject_command`'s body (lines 75-82):

```python
        if not hub._approval_store.claim(request_id, ApprovalStatus.REJECTED):
            raise HTTPException(status_code=400, detail="Approval request is not pending")
        await hub.resolve_approval(
            approval_req.worker_id, request_id, PolicyDecision(action="deny", reason=reason), approval_req.command
        )
        return {"status": "rejected"}
```

(The `if not approval_req: 404` check and the `await _require_admin(request)` call above stay exactly as they are. The old separate `hub._approval_store.resolve(...)` call is now removed — `claim` already moved the status.)

- [ ] **Step 6: Run the existing approval route + fanout suites for regression**

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_fanout_approval.py packages/provide-uterm-server/tests/bridge/test_easy_coverage_gaps_part2.py -vv`
Expected: PASS. If any test asserted the old `resolve()`-after-injection ordering, update it to assert claim-before-injection (the new, correct contract).

- [ ] **Step 7: Commit**

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/approvals.py \
        packages/provide-uterm-server/src/provide/uterm/server/routes/approvals.py \
        packages/provide-uterm-server/tests/bridge/test_approval_claim.py
git commit -m "fix(server): atomic claim closes approval resolve/reject double-injection TOCTOU"
```

---

## Task 4: Lock down the PAM notify socket (0o600)

**Why:** `PamNotifyListener.start()` creates the notify socket world-connectable (no `chmod`), unlike `CaptureSocket` which does `os.chmod(path, 0o600)`. Any local user forges login events that drive **root-side** session creation. Mirror the `CaptureSocket` pattern. (Review finding H-high; this single change blocks the unprivileged connection that both PAM highs depend on.)

**Files:**
- Modify: `packages/provide-uterm-platform/src/provide/uterm/pty/pam_listener.py:83-89`
- Test: `packages/provide-uterm-platform/tests/pty/test_pam_listener.py`

- [ ] **Step 1: Write the failing test**

Append to `test_pam_listener.py`:

```python
import os
import stat


async def test_notify_socket_is_owner_only(tmp_path) -> None:
    sock = tmp_path / "notify.sock"
    listener = PamNotifyListener(str(sock))
    await listener.start(AsyncMock())
    try:
        mode = stat.S_IMODE(os.stat(sock).st_mode)
        assert mode == 0o600
    finally:
        await listener.stop()
```

- [ ] **Step 2: Run it and confirm failure**

Run: `uv run pytest packages/provide-uterm-platform/tests/pty/test_pam_listener.py::test_notify_socket_is_owner_only -vv`
Expected: FAIL — socket mode is the umask default (e.g. `0o755`/`0o775`), not `0o600`.

- [ ] **Step 3: Add `os` import + chmod**

In `pam_listener.py`, ensure `import os` is present at the top of the imports. Then in `start` (line 88), after `start_unix_server`:

```python
        self._server = await asyncio.start_unix_server(self._handle_connection, path=self._path)
        # Restrict the notify socket to the owner so other local users cannot
        # forge login events that drive root-side session creation. Mirrors
        # CaptureSocket.start() in pty/capture.py.
        os.chmod(self._path, 0o600)  # noqa: PTH101 — chmod the just-bound socket fd path
        logger.info("pam_notify_listener started socket=%s", self._path)
```

- [ ] **Step 4: Run the test + full pam_listener suite**

Run: `uv run pytest packages/provide-uterm-platform/tests/pty/test_pam_listener.py -vv`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-platform/src/provide/uterm/pty/pam_listener.py \
        packages/provide-uterm-platform/tests/pty/test_pam_listener.py
git commit -m "fix(platform): restrict PAM notify socket to owner (0o600)"
```

> Deferred to the design-track (platform-specific, harder to test cross-OS): `SO_PEERCRED`/`LOCAL_PEERCRED` euid check on the notify connection, and confining `ev.capture_socket` to the configured `cap_dir` in `server/pam_integration.py:282`.

---

## Task 5: Recording files written owner-only (0o600)

**Why:** `start_session`/`append_events` open recording files with the default umask (often `0o644`) in a world-traversable dir; raw output may contain un-redacted secrets, bypassing the download-route authz. The repo already uses `chmod(0o600)` in `dev_idp.py`/`gateway`. (Review finding B-medium.)

**Files:**
- Modify: `packages/provide-uterm/src/provide/uterm/recording.py:134-149`
- Modify: `packages/provide-uterm/src/provide/uterm/session_logger.py` (LegacyFileStore `open("a")` sites)
- Test: `packages/provide-uterm/tests/test_recording_permissions.py` (new)

- [ ] **Step 1: Write the failing test**

Create `packages/provide-uterm/tests/test_recording_permissions.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import stat

from provide.uterm.recording import LocalFileRecordingStore


async def test_recording_file_is_owner_only(tmp_path) -> None:
    store = LocalFileRecordingStore(tmp_path)
    await store.start_session("sess1", {"k": "v"})
    path = tmp_path / "sess1.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
```

- [ ] **Step 2: Run it and confirm failure**

Run: `uv run pytest packages/provide-uterm/tests/test_recording_permissions.py -vv`
Expected: FAIL — file mode is the umask default, not `0o600`.

- [ ] **Step 3: chmod after each open in `recording.py`**

`start_session` (lines 137-138):

```python
            path.parent.mkdir(parents=True, exist_ok=True)
            f = path.open("a", encoding="utf-8")
            path.chmod(0o600)
```

`append_events` (lines 148-149):

```python
                path = self._get_path(session_id)
                f = path.open("a", encoding="utf-8")
                path.chmod(0o600)
```

- [ ] **Step 4: Apply the identical chmod in `session_logger.py`**

Find the two `open("a", encoding="utf-8")` calls in the `LegacyFileStore` class (around lines 59 and 62 — the `start_session`-equivalent and the lazy-open in the append path) and add `<path>.chmod(0o600)` immediately after each, using the `Path` object already in scope. The pattern is identical to Step 3.

- [ ] **Step 5: Run the test + the recording/registry suites**

Run: `uv run pytest packages/provide-uterm/tests/test_recording_permissions.py packages/provide-uterm-server/tests/server/test_registry.py -vv`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm/src/provide/uterm/recording.py \
        packages/provide-uterm/src/provide/uterm/session_logger.py \
        packages/provide-uterm/tests/test_recording_permissions.py
git commit -m "fix(core): write session recordings owner-only (0o600)"
```

---

## Task 6: Fix the inert `pip-audit` CI gate

**Why:** `ci.yml` runs `pip_audit --path .` — `--path` filters by *install path*, and nothing is installed at `.`, so it audits **0** packages and is always green. The repo's own `scripts/release_governance_check.sh:21` already uses the correct `--local` form (audits installed site-packages, skipping the not-yet-published workspace packages). (Review finding J-high.) This is a one-line workflow change — within the CLAUDE.md "≤3-line run block" policy.

**Files:**
- Modify: `.github/workflows/ci.yml:73`

- [ ] **Step 1: Verify the bug locally (the "failing test")**

Run: `uv run python -m pip_audit --path . --dry-run`
Expected output contains: `would have audited 0 packages`.
Then run: `uv run python -m pip_audit --local --dry-run`
Expected output contains: `would have audited` **a number > 100** (e.g. 168). This proves `--local` audits the real graph.

- [ ] **Step 2: Change the workflow line**

In `.github/workflows/ci.yml`, change line 73 from:

```yaml
      - run: uv run python -m pip_audit --path .
```

to:

```yaml
      # Audit the installed dependency graph (workspace packages aren't on PyPI
      # yet, so --local skips them with a notice). --path . audited zero packages.
      - run: uv run python -m pip_audit --local
```

- [ ] **Step 3: Verify the new command runs clean locally**

Run: `uv run python -m pip_audit --local`
Expected: exits 0 with "No known vulnerabilities found" (and audits the real packages). If it reports a real CVE, that is the gate working — open a separate dependency-bump issue; do **not** re-add `--path .` to hide it.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "fix(ci): pip-audit must scan the installed graph (--local), not an empty path"
```

---

## Task 7: Reject cleartext `http://` outbound URLs at config load

**Why:** All governance/IDP/JWKS outbound URL fields are bare `str` with no scheme check; an operator can configure `http://idp/jwks` and the server boots, sending HMAC secrets / auth headers / the admin-minting JWKS in cleartext. This is the **config-load scheme** half of the SSRF/egress work (the runtime DNS-resolution half is in the design track). Allow `http://` only for loopback (dev); require `https://` otherwise. (Review finding I-medium.)

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/config_schema.py` (helper + 2 validators)
- Test: `packages/provide-uterm-server/tests/server/test_outbound_url_scheme.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `packages/provide-uterm-server/tests/server/test_outbound_url_scheme.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest

from provide.uterm.server.config_schema import AuthConfig, GovernanceConfig


def test_cleartext_governance_url_to_remote_host_is_rejected() -> None:
    with pytest.raises(ValueError, match="https"):
        GovernanceConfig(policy_webhook_url="http://policy.internal/decide")


def test_https_governance_url_is_accepted() -> None:
    cfg = GovernanceConfig(policy_webhook_url="https://policy.internal/decide")
    assert cfg.policy_webhook_url == "https://policy.internal/decide"


def test_loopback_http_governance_url_is_allowed_for_dev() -> None:
    cfg = GovernanceConfig(authz_webhook_url="http://127.0.0.1:9000/authz")
    assert cfg.authz_webhook_url == "http://127.0.0.1:9000/authz"


def test_cleartext_idp_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="https"):
        AuthConfig(webhook_idp_url="http://idp.internal/resolve")
```

- [ ] **Step 2: Run them and confirm failure**

Run: `uv run pytest packages/provide-uterm-server/tests/server/test_outbound_url_scheme.py -vv`
Expected: FAIL — no scheme validation exists; the `ValueError` is never raised.

- [ ] **Step 3: Add the helper + validators to `config_schema.py`**

Add the import near the top (with the other stdlib imports):

```python
from urllib.parse import urlparse
```

Add a module-level helper (place it above `class GovernanceConfig`):

```python
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _require_secure_url(url: str | None, field_name: str) -> None:
    """Reject a cleartext ``http://`` outbound URL unless its host is loopback.

    ``https://`` is always allowed; ``http://`` is allowed only for loopback
    hosts (local dev). Any other scheme, or ``http://`` to a routable host,
    raises — these channels carry HMAC secrets, auth headers, and the JWKS
    used to validate admin tokens, so cleartext to a remote host is unsafe.
    """
    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http":
        raise ValueError(f"{field_name} must use http(s)")
    host = (parsed.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or host.endswith(".localhost"):
        return
    raise ValueError(f"{field_name} must use https:// (cleartext http:// is only allowed for loopback hosts)")
```

Add a validator to `GovernanceConfig` (after its fields):

```python
    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> GovernanceConfig:
        _require_secure_url(self.policy_webhook_url, "governance.policy_webhook_url")
        _require_secure_url(self.registry_webhook_url, "governance.registry_webhook_url")
        _require_secure_url(self.authz_webhook_url, "governance.authz_webhook_url")
        _require_secure_url(self.behavioral_audit_url, "governance.behavioral_audit_url")
        return self
```

Add a second validator to `AuthConfig` (it already has `_validate_proxy_secret`; pydantic runs multiple `mode="after"` validators):

```python
    @model_validator(mode="after")
    def _validate_outbound_url_schemes(self) -> AuthConfig:
        _require_secure_url(self.webhook_idp_url, "auth.webhook_idp_url")
        _require_secure_url(self.jwt_jwks_url, "auth.jwt_jwks_url")
        return self
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest packages/provide-uterm-server/tests/server/test_outbound_url_scheme.py -vv`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full server suite — fix any fixture that used cleartext remote URLs**

Run: `uv run pytest packages/provide-uterm-server/tests/ -q`
Expected: PASS. **If** a test fixture constructs a `GovernanceConfig`/`AuthConfig` with an `http://<non-loopback>` URL it will now fail validation — update those fixtures to `https://...` or `http://127.0.0.1...` (they were modeling an insecure config the validator now forbids).

- [ ] **Step 6: Commit**

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/config_schema.py \
        packages/provide-uterm-server/tests/server/test_outbound_url_scheme.py
git commit -m "fix(server): reject cleartext http:// for governance/IDP/JWKS URLs at config load"
```

---

## Final gate (after all 7 tasks)

- [ ] **Run the full quality gate** (lint, type, tests, coverage, complexity):

```bash
uv run python scripts/run_pytest_gate.py -q
```

Expected: all pass; **100% coverage maintained**. Then run the mutation gate on the changed perimeter files:

```bash
uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100
```

Expected: 100% kill rate on changed perimeter files. If a mutant survives, add a pinning test (e.g. for Task 2 assert *both* `fail_open` branches; for Task 7 assert the loopback-allow *and* remote-reject branches; for Task 3 assert claim returns False for both "missing" and "already-resolved").

---

## Design-first track (NOT in this plan — each needs brainstorming + its own plan)

These are real review findings but require a design decision; do not implement them mechanically.

1. **No-echo password masking (B-high).** There is **no** echo-state signal in the runtime today. Decide the source: PTY termios `ECHO` flag (local connector), ANSI `DECRST` echo mode (emulator), or the detector's `login_pass`/`game_pass`/`PROMPT_ECHO_OFF` classification (heuristic, stateful). Then route `runtime._log_send` through the existing `session_logger.log_send_masked(len(data))` while echo is off. **Acceptance:** a password typed at a no-echo prompt appears in the recording as `{"masked": true, ...}`, never as cleartext bytes.
2. **Snapshot/analysis role-scoped redaction (B-high).** `broadcast()` runs `StreamRedactor` only on `term` frames; `snapshot`/`analysis` (and the connect-time `last_snapshot`) leak un-redacted. Design a role-scoped redaction pass applied before broadcast **and** before storing `last_snapshot`. **Acceptance:** a secret scrubbed from the term stream does not appear in any viewer's snapshot.
3. **Resume-token atomic single-use (D-medium).** Add `consume(token) -> ResumeSession | None` to the `ResumeTokenStore` protocol that validates-and-revokes in one transaction (control-plane: conditional `UPDATE ... WHERE revoked_at IS NULL`, require rowcount==1; in-memory: pop-and-return). Replace the get-then-revoke pair in `browser_handlers`. **Acceptance:** two concurrent resumes with the same token → exactly one reclaims role/hijack.
4. **Connector/MCP runtime SSRF guard (C-high).** Factor a shared egress validator (DNS-resolve + private/loopback/link-local/metadata denylist, like `webhooks._delivery_url_allowed`) and enforce it at connector `start()` for ssh/telnet/ws **and** on the MCP `session_create` `url` host. **Acceptance:** `session_create(url="ws://169.254.169.254/...")` is rejected.
5. **PAM `SO_PEERCRED` + `capture_socket` path confinement (H-high follow-on).** Authenticate the notify peer's euid and confine the attacker-supplied `capture_socket` to `cap_dir`. Platform-specific; needs Linux/macOS handling.

## Larger follow-up plans (P1 / P2)

- **P1 plan — "Resource bounds & resilience":** per-send `broadcast()` timeout + stalled-viewer pruning; cumulative caps on all accumulating buffers (hold/input/IAC ×2/tunnel-WS/event-ring); per-principal connection quota; CF `_queue_bytes` finally-release; control-plane reaper + `VACUUM`; readiness flag; the missing metrics counters; CF token-hash reload decoupling.
- **P2 plan — "HA & architecture":** decide the horizontal-scale story (enforce single-active-instance vs. shared control plane + message bus); converge the FastAPI/Cloudflare lease-auth implementations behind a conformance contract; put `control/plane/sqlite/` on the mutation perimeter.

---

## Self-review

- **Spec coverage:** all 7 P0 items from the review's roadmap map to Tasks 1-7. The two P0 items that are design-dependent (no-echo masking; full SSRF) are explicitly carved out — masking → design-track #1; SSRF config-half → Task 7, runtime-half → design-track #4 — so nothing is silently dropped.
- **Placeholder scan:** every code step contains real, file-accurate code (constructor signatures, line numbers, and the `log_send_masked`/`CaptureSocket.chmod`/`release_governance_check.sh --local` precedents were read from source). The only "apply the same pattern" instruction (Task 5 Step 4, `session_logger.py`) is a byte-identical one-line `chmod` whose exact form is shown in Step 3.
- **Type/name consistency:** `claim(request_id, status) -> bool` (Task 3) is referenced identically in the store and both routes; `_require_secure_url(url, field_name)` (Task 7) is called with the same signature from both validators; `fail_open` (Task 2) is the same name in `__init__` and both return sites.
