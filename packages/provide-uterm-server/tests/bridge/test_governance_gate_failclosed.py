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


def test_governance_config_exposes_behavioral_fail_open_default_false() -> None:
    from provide.uterm.server.config_schema import GovernanceConfig

    assert GovernanceConfig().behavioral_fail_open is False
    assert GovernanceConfig(behavioral_fail_open=True).behavioral_fail_open is True


async def test_factory_passes_behavioral_fail_open_to_gate() -> None:
    from provide.uterm.server.app import create_server_app
    from provide.uterm.server.models import AuthConfig, GovernanceConfig, ServerConfig

    config = ServerConfig(
        auth=AuthConfig(mode="dev_token"),
        governance=GovernanceConfig(behavioral_audit_url="https://gov.example/audit", behavioral_fail_open=True),
    )
    app = create_server_app(config, api_only=True)
    gate = app.state.uterm_hub._behavioral_audit_gate
    assert isinstance(gate, WebhookBehavioralAuditGate)
    assert gate.fail_open is True
