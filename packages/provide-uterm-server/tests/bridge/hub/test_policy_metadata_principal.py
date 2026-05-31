#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""M2 regression: governance gates must not crash on a Principal in metadata.

``prepare_policy_context`` previously put the raw :class:`Principal` dataclass
into ``PolicyContext.metadata``. The webhook governance gates ``json.dumps`` the
metadata, so a raw Principal raised
``TypeError: Object of type Principal is not JSON serializable`` — which was
uncaught and tore down the browser WS session on every keystroke when
governance was enabled.

The fix projects the Principal to an allow-listed JSON-safe dict containing
only ``{subject_id, roles}`` — never the full claims/scopes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocket

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.hub.ext import (
    PolicyContext,
    WebhookPolicyGate,
    _encode_webhook_payload,
)
from provide.uterm.server.bridge.identity import IdentityProvider, Principal


def _hub_with_principal(principal: Principal) -> tuple[TermHub, MagicMock]:
    mock_idp = MagicMock(spec=IdentityProvider)
    mock_idp.resolve_principal = AsyncMock(return_value=principal)
    hub = TermHub(identity_provider=mock_idp)
    mock_ws = MagicMock(spec=WebSocket)
    return hub, mock_ws


@pytest.mark.asyncio
async def test_policy_context_metadata_principal_is_json_safe_projection() -> None:
    """metadata['principal'] is an allow-listed {subject_id, roles} dict, not the raw object."""
    principal = Principal(
        subject_id="user-123",
        roles=frozenset({"operator", "admin"}),
        scopes=frozenset({"secret:scope"}),
        claims={"secret_claim": "do-not-leak"},
    )
    hub, mock_ws = _hub_with_principal(principal)

    context = await hub.prepare_policy_context(mock_ws, "worker1")

    assert context.metadata is not None
    projected = context.metadata["principal"]
    # Only the allow-listed fields are present.
    assert projected == {"subject_id": "user-123", "roles": ["admin", "operator"]}
    # Sensitive fields must never be projected.
    assert "scopes" not in projected
    assert "claims" not in projected
    # subject_id is stringified, roles sorted for determinism.
    assert projected["subject_id"] == "user-123"
    assert projected["roles"] == sorted(projected["roles"])
    # client_id derivation is unchanged.
    assert context.client_id == "user-123"


@pytest.mark.asyncio
async def test_policy_context_metadata_is_json_serializable() -> None:
    """The whole metadata dict round-trips through json.dumps (the gate's encode step)."""
    principal = Principal(subject_id="user-xyz", roles=frozenset({"viewer"}))
    hub, mock_ws = _hub_with_principal(principal)

    context = await hub.prepare_policy_context(mock_ws, "worker1")

    payload = {
        "worker_id": context.worker_id,
        "client_id": context.client_id,
        "metadata": context.metadata,
    }
    # Must NOT raise TypeError: Object of type Principal is not JSON serializable.
    body = _encode_webhook_payload(payload)
    assert b"user-xyz" in body
    assert b"viewer" in body
    # The raw Principal repr must never reach the wire.
    assert b"Principal(" not in body


@pytest.mark.asyncio
async def test_webhook_gate_post_body_serializes_with_principal(respx_mock) -> None:
    """End-to-end: the WebhookPolicyGate POST body is JSON-serializable with a Principal present.

    Before the fix this gate path raised TypeError inside json.dumps and the
    exception propagated out, tearing down the WS session. The gate's own
    try/except catches transport errors but the encode happens *before* the
    try, so a serialization TypeError was uncaught.
    """
    import httpx

    captured: dict[str, bytes] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"action": "allow"})

    respx_mock.post("https://gov.example/policy").mock(side_effect=_record)

    principal = Principal(
        subject_id="op-1",
        roles=frozenset({"operator"}),
        scopes=frozenset({"never:leak"}),
        claims={"never": "leak"},
    )
    hub, mock_ws = _hub_with_principal(principal)
    context: PolicyContext = await hub.prepare_policy_context(mock_ws, "worker1", action="input")

    gate = WebhookPolicyGate(url="https://gov.example/policy")
    decision = await gate.intercept_input("ls -la\n", context)

    assert decision.action == "allow"
    body = captured["body"]
    assert b"op-1" in body
    assert b"operator" in body
    # Allow-listed projection only — scopes/claims never on the wire.
    assert b"never" not in body
    assert b"Principal(" not in body
