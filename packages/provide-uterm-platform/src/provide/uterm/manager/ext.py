#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Protocol, runtime_checkable

import httpx
from provide.telemetry import event

# Standardized DAS Events for Agent Lifecycle
EVENT_AGENT_SPAWNED = event("terminal", "agent", "spawned")
EVENT_AGENT_EXITED = event("terminal", "agent", "exited")
EVENT_AGENT_KILLED = event("terminal", "agent", "killed")


@runtime_checkable
class AgentSpawnPolicyGate(Protocol):
    """Protocol for external agent spawn policy enforcement."""

    async def intercept_spawn(self, agent_id: str, config_path: str, raw_config: dict[str, Any]) -> bool:
        """Return True to allow the spawn, False to reject."""
        ...


class NoOpAgentSpawnPolicyGate(AgentSpawnPolicyGate):
    """Default policy gate that allows all spawns."""

    async def intercept_spawn(self, agent_id: str, config_path: str, raw_config: dict[str, Any]) -> bool:
        _ = (agent_id, config_path, raw_config)
        return True


class WebhookAgentSpawnPolicyGate(AgentSpawnPolicyGate):
    """Policy gate that delegates spawn decisions to an external webhook."""

    def __init__(self, url: str, secret: str | None = None, timeout_s: float = 2.0):
        self.url = url
        self.secret = secret
        self.timeout = timeout_s

    async def intercept_spawn(self, agent_id: str, config_path: str, raw_config: dict[str, Any]) -> bool:
        payload = {
            "agent_id": agent_id,
            "config_path": config_path,
            "raw_config": raw_config,
        }
        # Serialize the body deterministically so the signature covers exactly
        # the bytes on the wire. When a secret is configured, sign with
        # HMAC-SHA256 and send it in X-Signature so the webhook can verify the
        # request actually came from this manager.
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.secret:
            digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Signature"] = f"sha256={digest}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, content=body, headers=headers)
                if resp.status_code == 200:
                    return bool(resp.json().get("allow", False))
                return False
        except Exception:
            return False
