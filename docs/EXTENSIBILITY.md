# Node Extensibility: Gating & Telemetry API

This document describes the hooks available in the `provide-uterm` Node (Hub) and Supervisor (Agent Manager) for integration with an external management tier (e.g., External Management Tier).

## 1. Node Discovery (Heartbeat)

If `governance.registry_webhook_url` is configured, the Node will periodically send a POST request with its current status.

**Payload:**
```json
{
  "node_id": "string",
  "active_sessions": 0,
  "worker_count": 0,
  "timestamp": 1713634000.0
}
```

## 2. Terminal Input Gating (Policy Gate)

If `governance.policy_webhook_url` is configured, every terminal input event is intercepted and sent to the webhook for an allow/deny decision.

**Payload:**
```json
{
  "worker_id": "string",
  "client_id": "string",
  "role": "string",
  "action": "input",
  "data": "raw_terminal_input",
  "metadata": {
    "principal": { ... }
  }
}
```

**Expected Response:**
```json
{
  "allow": true
}
```

## 3. Agent Spawn Gating (Supervisor Tier)

If `manager.spawn_policy_webhook_url` is configured, the Supervisor tier will consult the webhook before spawning any new bot process.

**Payload:**
```json
{
  "agent_id": "string",
  "config_path": "path/to/config.yaml",
  "raw_config": { ... }
}
```

**Expected Response:**
```json
{
  "allow": true
}
```

## 4. Pluggable Authorization

If `governance.authz_webhook_url` is configured, the `AuthorizationService` will delegate all capability and role checks to the external service.

**Payload:**
```json
{
  "principal": {
    "subject_id": "string",
    "roles": [],
    "scopes": [],
    "claims": {}
  },
  "action": "session.read",
  "context": {
    "session_id": "string"
  }
}
```

**Expected Response:**
```json
{
  "allow": true
}
```

## 5. Standardized Telemetry (DAS)

All architectural tiers emit standardized Domain-Action-Status (DAS) events via the `provide-telemetry` logger.

**Standard Event List:**
- `terminal.session.registered`
- `terminal.session.disconnected`
- `terminal.hijack.acquired`
- `terminal.hijack.released`
- `terminal.hijack.expired`
- `terminal.ratelimit.triggered`
- `terminal.agent.spawned`
- `terminal.agent.exited`
- `terminal.agent.killed`

## 6. Raw Byte Tapping for Session Observers

For any transport session that supports ``add_watch`` (`TelnetSession`, `WebSocketSession`, and derived classes), wire bytes are available through one hook:

```python
session = await connect_telnet("twgs.example.com", 2300)
buf: list[bytes] = []
session.add_watch(lambda _state, raw: buf.append(raw))
```

Use ``session.add_watch`` when you need colors/escape-sequences/cp437 bytes for replay, telemetry, or screen snapshots. Use ``snapshot()`` / ``ansi_screen()`` for parsed display state.

If you see advice to monkey-patch the emulator in older consumer code, this is the replacement path.
