<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# P0.5 — Remaining Mechanical Highs Implementation Plan

> **STATUS (2026-06-01): MERGED.** All P0.5 remaining-high tasks are complete on local `main`. Unchecked
> `- [ ]` boxes below are historical; authoritative end-state: `docs/coverage-audit-2026-06-01.md`
> (all 16 HIGH findings fixed + code-spot-checked, 0 of 83 open).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Close four more high/medium findings from the 2026-05-29 review (`docs/enterprise-hardening-review-2026-05-29.md`) that are mechanical or safely-gated, building on the merged P0 work (`main` @ `6061101`).

**Architecture:** Surgical, per-task changes, each one cohesive change + tests + one commit. 100% branch coverage + mutation testing are enforced — every new branch needs a test that kills mutants. `asyncio_mode="auto"` (async tests need no marker). `respx` for HTTP mocking.

**Deferred to their own plans (NOT here):**
- **CF tunnel-token reload** (the `_meta_loaded` revocation/hibernation HIGH×2) — separate subsystem (Durable Object lifecycle) with its own test harness; warrants a dedicated plan.
- **Webhook replay-protection + cleartext-secret removal** (HIGH) — changes the signed wire format; needs a receiver-compat decision (deprecation path) before coding.
- **Telnet client-side `_rx_buf` cap** (MED) — sibling to Task 1 but needs the `receive()`/caller error-handling path verified; fold into a follow-up once the gateway cap lands.

---

## File Structure

| File | Change | Task |
|---|---|---|
| `packages/provide-uterm-server/src/provide/uterm/gateway/_iac_negotiate.py` | Cap the IAC subnegotiation buffer | 1 |
| `packages/provide-uterm-server/src/provide/uterm/server/config_schema.py` | `behavioral_fail_open` field | 2 |
| `packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py` | Pass `fail_open` to the gate (T2); deny ad-hoc observers (T4) | 2, 4 |
| `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/router_impl.py` | Per-send timeout in `broadcast()` | 3 |
| `packages/provide-uterm-server/src/provide/uterm/server/config_schema.py` | `allow_adhoc_browser_observers` field | 4 |

---

## Task 1: Cap the telnet IAC subnegotiation buffer (gateway)

**Why:** `IacNegotiator.feed()` buffers every byte after `IAC SB <opt>` into `_sb_buf` until an `IAC SE` arrives, with no upper bound. The telnet gateway accepts raw TCP with no pre-auth, so an unauthenticated client that opens a subnegotiation and never closes it grows `_sb_buf` unboundedly → memory-exhaustion DoS. (Review finding E-high.)

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/gateway/_iac_negotiate.py` (the two `self._sb_buf.append(...)` sites in `feed()`, ~lines 226 & 229; add a module constant + helper)
- Test: `packages/provide-uterm-server/tests/gateway/test_iac_negotiate.py`

- [ ] **Step 1: Write the failing test** (append to `test_iac_negotiate.py`; mirror its existing `IacNegotiator` construction):

```python
def test_unterminated_subnegotiation_is_bounded() -> None:
    from provide.uterm.server.gateway._iac_negotiate import _MAX_SB_BYTES, IacNegotiator

    neg = IacNegotiator()
    # Open a subnegotiation, then stream far more bytes than the cap with no IAC SE.
    neg.feed(b"\xff\xfa\x18")  # IAC SB TTYPE
    neg.feed(b"A" * (_MAX_SB_BYTES * 4))
    # Buffer must never exceed the cap, and the runaway SB is abandoned.
    assert len(neg._sb_buf) <= _MAX_SB_BYTES
    assert neg._sb_option is None
```

- [ ] **Step 2: Run it, confirm FAIL**

Run: `uv run pytest packages/provide-uterm-server/tests/gateway/test_iac_negotiate.py::test_unterminated_subnegotiation_is_bounded -vv`
Expected: FAIL — `_MAX_SB_BYTES` does not exist (ImportError) / buffer grows unbounded.

- [ ] **Step 3: Add the constant + bounding helper, route the two append sites through it**

Add a module-level constant near the other module constants in `_iac_negotiate.py`:

```python
# Max bytes buffered for a single IAC subnegotiation before it is abandoned.
# Legitimate TTYPE / NEW-ENVIRON payloads are tiny; 4 KiB is far above any
# real value and bounds a hostile client that opens `IAC SB` and never sends
# `IAC SE` (unbounded-memory DoS on the unauthenticated telnet gateway).
_MAX_SB_BYTES = 4096
```

Add a helper method on `IacNegotiator` (near `_finish_sb`):

```python
    def _append_sb(self, byte: int) -> None:
        """Buffer a subnegotiation byte, abandoning the SB if it grows too large.

        Past ``_MAX_SB_BYTES`` the subnegotiation is discarded and SB state is
        reset so a client that never sends ``IAC SE`` cannot grow ``_sb_buf``
        without bound.
        """
        if len(self._sb_buf) >= _MAX_SB_BYTES:
            self._sb_option = None
            self._sb_buf = bytearray()
            return
        self._sb_buf.append(byte)
```

In `feed()`, replace the two `self._sb_buf.append(...)` calls (the escaped-IAC path and the normal-byte path) with `self._append_sb(...)`:

```python
                if data[i] == _IAC and i + 1 < n and data[i + 1] == _IAC:
                    self._append_sb(_IAC)
                    i += 2
                    continue
                self._append_sb(data[i])
                i += 1
                continue
```

- [ ] **Step 4: Run the test + the full gateway IAC suite**

Run: `uv run pytest packages/provide-uterm-server/tests/gateway/test_iac_negotiate.py -vv`
Expected: PASS (new test + all existing). Add a second test that a *legitimate* small subnegotiation still parses (`IAC SB TTYPE IS "xterm" IAC SE` → `neg.term == "xterm"`) if one isn't already present, so the cap doesn't regress normal parsing.

- [ ] **Step 5: Lint + commit**

Run: `uv run ruff check packages/provide-uterm-server/src/provide/uterm/gateway/_iac_negotiate.py` (clean).

```bash
git add packages/provide-uterm-server/src/provide/uterm/gateway/_iac_negotiate.py \
        packages/provide-uterm-server/tests/gateway/test_iac_negotiate.py
git commit -m "fix(gateway): cap telnet IAC subnegotiation buffer to bound memory DoS"
```

---

## Task 2: Wire `fail_open` through `GovernanceConfig`

**Why:** P0 made `WebhookBehavioralAuditGate` fail closed with a programmatic `fail_open` kwarg, but it isn't reachable from config — operators who need fail-open for availability can't set it. (Review follow-up to F-medium.)

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/config_schema.py` (`GovernanceConfig` — add `behavioral_fail_open`)
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py` (pass it when constructing the gate, ~line 459)
- Test: `packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py`

- [ ] **Step 1: Write the failing test** (append):

```python
def test_governance_config_exposes_behavioral_fail_open_default_false() -> None:
    from provide.uterm.server.config_schema import GovernanceConfig

    assert GovernanceConfig().behavioral_fail_open is False
    assert GovernanceConfig(behavioral_fail_open=True).behavioral_fail_open is True
```

- [ ] **Step 2: Run it, confirm FAIL** (`AttributeError: behavioral_fail_open`).

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py::test_governance_config_exposes_behavioral_fail_open_default_false -vv`

- [ ] **Step 3: Add the field to `GovernanceConfig`** (next to the other `behavioral_*` fields):

```python
    behavioral_fail_open: bool = False
```

- [ ] **Step 4: Pass it through in `factory_impl.py`** where `WebhookBehavioralAuditGate` is constructed (~line 459):

```python
        behavioral_audit_gate = WebhookBehavioralAuditGate(
            url=config.governance.behavioral_audit_url,
            secret=config.governance.behavioral_audit_secret,
            fail_open=config.governance.behavioral_fail_open,
        )
```

- [ ] **Step 5: Add an integration test** asserting the wiring (append to the same test file):

```python
def test_factory_passes_behavioral_fail_open_to_gate() -> None:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.models import AuthConfig, GovernanceConfig, ServerConfig

    config = ServerConfig(
        auth=AuthConfig(mode="dev_token"),
        governance=GovernanceConfig(behavioral_audit_url="https://gov.example/audit", behavioral_fail_open=True),
    )
    app = create_server_app(config, api_only=True)
    gate = app.state.uterm_hub.behavioral_audit_gate
    assert gate is not None
    assert gate.fail_open is True
```

(Confirm the hub attribute name exposing the gate — likely `behavioral_audit_gate`; if it differs, read `factory_impl.py`/the hub and use the correct accessor.)

- [ ] **Step 6: Run + lint + commit**

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py -vv` → all pass.
Run: `uv run ruff check` the two source files → clean.

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/config_schema.py \
        packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py \
        packages/provide-uterm-server/tests/bridge/test_governance_gate_failclosed.py
git commit -m "feat(server): expose behavioral-audit fail_open via GovernanceConfig"
```

---

## Task 3: Per-send timeout in `broadcast()`

**Why:** `broadcast()` (router_impl.py:151) does `await ws.send_text(final_payload)` with no timeout; a viewer whose TCP receive window is stalled blocks the worker-output fanout indefinitely (head-of-line blocking) and is never pruned (pruning only fires on an exception, not a stall). (Review finding E-high — minimal fix; the full queue-per-browser rework is P1.)

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/router_impl.py` (the send at ~line 151; ensure `import asyncio`)
- Test: `packages/provide-uterm-server/tests/bridge/test_broadcast_timeout.py` (new)

- [ ] **Step 1: Write the failing test** (new file). A browser whose `send_text` hangs forever must be pruned once the per-send timeout elapses:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import asyncio

import provide.uterm.server.bridge.hub.router_impl as router_impl


class _HangingWS:
    """A browser WS whose send_text never completes (stalled receive window)."""

    def __init__(self) -> None:
        self.client_state = None

    async def send_text(self, _payload: str) -> None:
        await asyncio.Event().wait()  # hang forever


async def test_broadcast_prunes_a_browser_whose_send_stalls(make_hub_with_browser, monkeypatch) -> None:
    # Drive the per-send timeout down so the test is fast.
    monkeypatch.setattr(router_impl, "_BROADCAST_SEND_TIMEOUT_S", 0.05)
    hub, worker_id, ws = make_hub_with_browser(_HangingWS())

    await hub.broadcast(worker_id, {"type": "term", "data": "x"})

    # The stalled browser was treated as dead and removed from the worker state.
    st = hub.registry.get(worker_id)
    assert ws not in st.browsers
```

> `make_hub_with_browser` is a helper you must build (or reuse) that constructs a `TermHub`, registers a worker, and registers the given fake `ws` as a browser for it. **Read `tests/bridge/test_hub.py` and `conftest*.py` first** — there is almost certainly an existing hub fixture + a `register_browser`/`register_worker` path to mirror; if a reusable helper exists, use it instead of adding `make_hub_with_browser`. Keep the test asserting the same behavior (stalled browser pruned).

- [ ] **Step 2: Run it, confirm FAIL** — `_BROADCAST_SEND_TIMEOUT_S` doesn't exist yet, and without the timeout the broadcast hangs forever (the test would itself time out → that *is* the failing signal; if your harness lacks a hang-guard, the missing attribute on `monkeypatch.setattr` fails first).

- [ ] **Step 3: Implement the per-send timeout**

Ensure `import asyncio` is present at the top of `router_impl.py`. Add a module constant near the top:

```python
# Per-browser send timeout in broadcast(). A viewer whose receive window is
# stalled is treated as dead and pruned rather than head-of-line-blocking the
# worker-output fanout indefinitely.
_BROADCAST_SEND_TIMEOUT_S = 5.0
```

Change the send line (~151) from `await ws.send_text(final_payload)` to:

```python
                await asyncio.wait_for(ws.send_text(final_payload), timeout=_BROADCAST_SEND_TIMEOUT_S)
```

The existing `except Exception` already adds the ws to `dead` and the existing `remove_dead_browsers` call prunes it — `asyncio.wait_for` raises `TimeoutError` (an `Exception`), so a stalled browser now joins `dead` instead of hanging. No other change.

- [ ] **Step 4: Run the new test + the broadcast/hub regression suites**

Run: `uv run pytest packages/provide-uterm-server/tests/bridge/test_broadcast_timeout.py packages/provide-uterm-server/tests/bridge/test_hub.py -vv`
Expected: PASS. The happy-path (fast send) tests in `test_hub.py` must still pass — the timeout never fires for a prompt send.

- [ ] **Step 5: Lint + commit**

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/router_impl.py \
        packages/provide-uterm-server/tests/bridge/test_broadcast_timeout.py
git commit -m "fix(server): bound broadcast per-send with a timeout and prune stalled viewers"
```

---

## Task 4: Deny ad-hoc-worker observers by default (with opt-out)

**Why:** `_resolve_browser_role` returns a role from the principal's claim (admin/operator/viewer) when a worker has **no** registered `SessionDefinition` (ad-hoc), skipping the `can_read_session` visibility check — so any authenticated viewer who knows/guesses an ad-hoc `worker_id` (`^[\w\-]+$`) can observe its stream. Fail closed: only a global admin may observe an unregistered worker, with an explicit opt-out config flag for deployments that intentionally rely on ad-hoc observation. (Review finding A-high.)

> **Behavior change to call out in the PR:** with the default (`auth.allow_adhoc_browser_observers=False`), operator/viewer principals can no longer attach a browser to a worker that has no registered `SessionDefinition` — they get `WS 1008`. Deployments that relied on ad-hoc observation set `auth.allow_adhoc_browser_observers=true` to restore the prior behavior.

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/config_schema.py` (`AuthConfig` — add the flag)
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py` (`_resolve_browser_role`, ~lines 415-425)
- Test: `packages/provide-uterm-server/tests/server/test_idp_app_task5.py` (update the 2 existing ad-hoc tests + add deny/opt-out tests)

- [ ] **Step 1: Update + add tests** in `test_idp_app_task5.py`.

The existing `test_resolve_browser_role_webhook_idp_none_principal_is_anonymous_viewer` currently expects `role == "viewer"` for an ad-hoc worker — under the new default that path is **denied**. Change it to assert a `WebSocketException` is raised, and add an opt-out test + keep the admin test:

```python
async def test_resolve_browser_role_adhoc_non_admin_denied_by_default(monkeypatch) -> None:
    """Default: a non-admin principal cannot observe an unregistered (ad-hoc) worker."""
    from types import SimpleNamespace

    from fastapi import WebSocketException

    config = ServerConfig(
        auth=AuthConfig(identity_provider="webhook", mode="dev_token", webhook_idp_url="http://localhost:8080/auth")
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _viewer(_ws):
        return Principal(subject_id="bob", roles=frozenset({"viewer"}), scopes=frozenset())

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _viewer)
    ws = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(WebSocketException):
        await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-deny")


async def test_resolve_browser_role_adhoc_viewer_allowed_when_opted_in(monkeypatch) -> None:
    """With the opt-out flag, the legacy honor-the-claim behavior returns viewer."""
    from types import SimpleNamespace

    config = ServerConfig(
        auth=AuthConfig(
            identity_provider="webhook",
            mode="dev_token",
            webhook_idp_url="http://localhost:8080/auth",
            allow_adhoc_browser_observers=True,
        )
    )
    app = create_server_app(config, api_only=True)
    hub = app.state.uterm_hub

    async def _viewer(_ws):
        return Principal(subject_id="bob", roles=frozenset({"viewer"}), scopes=frozenset())

    monkeypatch.setattr(app.state.uterm_idp, "resolve_principal", _viewer)
    ws = SimpleNamespace(state=SimpleNamespace())
    assert await hub.resolve_role_for_browser(ws, "ad-hoc-unregistered-optin") == "viewer"
```

Also update the existing `test_resolve_browser_role_webhook_idp_none_principal_is_anonymous_viewer`: a `None` principal on an ad-hoc worker is now denied too — change it to wrap the call in `with pytest.raises(WebSocketException):` (the anonymous viewer is non-admin). Keep `test_resolve_browser_role_webhook_idp_principal_role_honored` (admin) returning `"admin"` (admins are always allowed). Ensure `import pytest` is present.

- [ ] **Step 2: Run, confirm the new deny tests FAIL** (current code returns viewer, no flag exists).

Run: `uv run pytest packages/provide-uterm-server/tests/server/test_idp_app_task5.py -vv`

- [ ] **Step 3: Add the config flag** to `AuthConfig` in `config_schema.py` (near the other auth toggles):

```python
    # When a worker has no registered SessionDefinition (ad-hoc), browser
    # observers are denied by default — only a global admin may observe. Set
    # this True to restore the legacy behavior of honoring the principal's
    # role claim for unregistered workers.
    allow_adhoc_browser_observers: bool = False
```

- [ ] **Step 4: Fail closed in `_resolve_browser_role`** (factory_impl.py). Replace the `if session is None:` block (currently honoring admin/operator/viewer claims) with:

```python
        session = await registry.get_definition(worker_id) if registry is not None else None
        if session is None:
            # No registered SessionDefinition (worker connected ad-hoc). There
            # is no visibility policy to consult, so fail closed: only a global
            # admin may observe an unregistered worker. Operators/viewers are
            # rejected unless the operator explicitly opts in.
            if "admin" in principal.roles:
                return "admin"
            if config.auth.allow_adhoc_browser_observers:
                if "operator" in principal.roles:
                    return "operator"
                return "viewer"
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="insufficient privileges")
        if not await authz.can_read_session(principal, session):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="insufficient privileges")
        return await policy.role_for(principal, session)
```

(`WebSocketException` and `status` are already imported in this module — confirm. `config` is in the closure scope.)

- [ ] **Step 5: Run the full server suite** — the deny is a real behavior change; find any test that attaches a non-admin browser to an ad-hoc worker and either set `allow_adhoc_browser_observers=True` in its fixture (if it intends to test ad-hoc observation) or update it to expect the deny:

Run: `uv run pytest packages/provide-uterm-server/tests/ -q 2>&1 | tail -25`
Fix each genuine break surgically (URL/flag in the fixture, or assert the deny). List every test touched.

- [ ] **Step 6: Coverage of the new branches** — confirm the admin / opt-in-operator / opt-in-viewer / deny branches are all covered:

Run: `uv run pytest packages/provide-uterm-server/tests/server/test_idp_app_task5.py -q --cov=provide.uterm.server.app.factory_impl --cov-report=term-missing --no-cov-on-fail 2>&1 | tail -6` — the new `if session is None` branches must not be in "Missing".

- [ ] **Step 7: Lint + commit**

```bash
git add packages/provide-uterm-server/src/provide/uterm/server/config_schema.py \
        packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py \
        packages/provide-uterm-server/tests/server/test_idp_app_task5.py \
        <any fixture tests you adjusted in Step 5>
git commit -m "fix(server): deny ad-hoc-worker browser observers by default (admin-only, opt-out flag)"
```

---

## Final gate (after all 4 tasks)

- [ ] **Authoritative full multi-package gate** (NOT per-file pytest — that masks coverage gaps):

```bash
uv run python scripts/run_all_tests.py
```

Expected: "All package test suites passed", every package 100% coverage, 0 failures. Then mutation gate on changed perimeter files:

```bash
uv run python scripts/run_mutation_gate.py --changed-only --min-mutation-score 100
```

If a mutant survives, add a pinning test (e.g. Task 1: assert the *legit* small SB still parses AND the oversized one is dropped; Task 4: assert all four `session is None` branches).

---

## Self-review

- **Spec coverage:** Tasks 1-4 map to review findings E-high (telnet IAC), F-medium follow-up (`fail_open` config), E-high (broadcast HOL), A-high (ad-hoc authz). The other remaining highs (CF token-reload, webhook replay, telnet client cap) are explicitly deferred with reasons.
- **Placeholder scan:** every code step has real, file-accurate code read from source (`router_impl.broadcast` send loop, `_iac_negotiate.feed` append sites, `_resolve_browser_role` block, `GovernanceConfig`/`AuthConfig` fields, `resolve_role_for_browser` re-raising `WebSocketException`). The only soft spot — the broadcast-test hub fixture (`make_hub_with_browser`) — is explicitly flagged to mirror `test_hub.py`'s existing registration path.
- **Type/name consistency:** `_BROADCAST_SEND_TIMEOUT_S` (Task 3), `behavioral_fail_open` (Task 2), `allow_adhoc_browser_observers` (Task 4) are each referenced identically in source + tests.
