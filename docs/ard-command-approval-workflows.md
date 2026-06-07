# ARD: Command Approval Workflows

## Problem

In high-stakes environments — production infrastructure, regulated systems, disaster recovery — certain commands are too dangerous to execute without a second pair of eyes. Existing solutions require operators to context-switch to a separate approval system (Jira, ServiceNow, Slack) *after* they have already typed the command, creating a race between intent and execution.

provide-uterm holds the hijack input stream: it is the only place in the stack that can intercept a command *before* it reaches the remote host. A policy-driven approval gate at this layer can hold a command for async human approval without any changes to the remote host, the operator's terminal client, or the worker.

---

## Goals

- Intercept input events that match a configurable policy before forwarding to the worker.
- Hold the terminal in a suspended state while an approval request is dispatched (webhook, Slack, REST callback).
- Forward the command automatically on approval; discard it on rejection or timeout.
- Emit a structured `ApprovalEvent` to the audit trail regardless of outcome.
- Impose zero overhead on non-matching commands (the common case).
- Support both synchronous (blocking) and asynchronous (webhook callback) approval flows.
- Degrade gracefully: if the approval service is unreachable, apply a configurable default policy (fail-open or fail-closed).

---

## Non-Goals

- Replacing a change management system (ServiceNow, Jira). This is a real-time gate, not a ticket system.
- Blocking output from the remote host while an approval is pending (output continues to flow).
- Modifying or redacting the command (approved as-is, or rejected outright).

---

## Architecture

### Policy Interface

```python
@dataclass(slots=True)
class ApprovalPolicy:
    policy_id: str
    name: str
    pattern: str               # regex matched against raw input data
    min_role: str              # minimum role required to trigger ("operator" | "admin")
    approver_url: str          # webhook URL for approval requests
    timeout_s: float           # how long to hold before applying default_action
    default_action: str        # "approve" | "reject" — applied on timeout or service failure
    require_different_user: bool = True   # approver cannot be the same principal as submitter
    notify_browsers: bool = True          # send hold/release messages to all connected browsers

@dataclass(slots=True)
class ApprovalRequest:
    request_id: str            # uuid4
    policy_id: str
    session_id: str
    worker_id: str
    principal: str             # subject_id of the browser that submitted the command
    role: str
    command: str               # the full input string
    submitted_at: float
    expires_at: float
    status: str                # "pending" | "approved" | "rejected" | "timeout" | "cancelled"
    approver: str | None       # subject_id of the approver (if resolved)
    decided_at: float | None

@dataclass(slots=True)
class ApprovalEvent:
    request_id: str
    policy_id: str
    outcome: str               # "approved" | "rejected" | "timeout" | "cancelled"
    principal: str
    approver: str | None
    command: str
    duration_s: float
```

> **As-built (2026-06):** the shipped model is leaner. There is no
> `ApprovalPolicy`, `ApprovalEvent`, or `ApprovalEventSink`. Policy gating is
> done by the `PolicyGate` protocol returning a `PolicyDecision`
> (`action` ∈ `{allow, deny, hold}`, plus `request_id`, `timeout_s`,
> `reason`) over a `PolicyContext`, all in `server/bridge/hub/ext.py` — not by
> per-policy regex `ApprovalPolicy` objects. The shipped `ApprovalRequest`
> (`server/bridge/hub/approvals.py`) has fields
> `id, worker_id, submitter_id, command, status, created_at, expires_at,
> group_id, is_fanout` — not the
> `request_id`/`policy_id`/`session_id`/`principal`/`role`/`submitted_at`/
> `approver`/`decided_at` shown here. `status` is the `ApprovalStatus` enum.

### Approval Gate

```python
class CommandApprovalGate:
    def __init__(self, policies: list[ApprovalPolicy], sink: ApprovalEventSink) -> None: ...

    async def intercept(
        self,
        worker_id: str,
        data: str,
        *,
        principal: str,
        role: str,
        hub: TermHub,
    ) -> bool:
        """Return True if the command should be forwarded immediately.
        Return False if it has been held (pending approval) or rejected.
        """
```

On a policy match, `intercept()`:
1. Creates an `ApprovalRequest` with a unique `request_id`.
2. Pauses the worker (sends `control:pause` via `hub.send_worker`).
3. Dispatches an approval webhook (HTTP POST) with the request payload.
4. Broadcasts a `{"type": "approval_pending", "request_id": ..., "command": ..., "expires_at": ...}` message to all connected browsers.
5. Awaits resolution via a per-request `asyncio.Event`, up to `timeout_s`.
6. On approval: forwards the command and resumes the worker.
7. On rejection or timeout: discards the command and resumes the worker.
8. Emits an `ApprovalEvent` to the sink in all cases.

Non-matching commands return `True` immediately (zero overhead path).

> **As-built (2026-06):** there is no `CommandApprovalGate` class. The shipped
> interception interface is `PolicyGate.intercept_input(self, data, context:
> PolicyContext) -> PolicyDecision` (`server/bridge/hub/ext.py`), with
> implementations `NoOpPolicyGate` (default, allows everything) and
> `WebhookPolicyGate` (delegates to an external webhook). A `hold` decision
> creates an `ApprovalRequest` and pauses; the `deny`/`allow` actions
> short-circuit. There is no regex `policies=[...]` list construction.

### REST Callback Endpoints

```
POST /api/approvals/{request_id}/approve
    Body: {"approver": "subject_id", "note": "optional"}
    → 200 OK | 404 Not Found | 409 Conflict (already decided)

POST /api/approvals/{request_id}/reject
    Body: {"approver": "subject_id", "reason": "optional"}
    → 200 OK | 404 Not Found | 409 Conflict

GET /api/approvals/{request_id}
    → ApprovalRequest (current status)

GET /api/approvals?session_id=...&status=pending
    → list[ApprovalRequest]
```

These endpoints require admin role. `require_different_user` is enforced server-side by comparing `approver` against `request.principal`.

> **As-built (2026-06):** the shipped routes (`server/routes/approvals.py`) are
> `POST /api/approvals/{request_id}/approve` and `.../reject`, plus
> `GET /api/approvals` which lists **pending** requests only (no `session_id`
> or `status` query filters). A double-decide on a non-pending request returns
> **400** ("Approval request is not pending"), not 409. There is no
> single-resource `GET /api/approvals/{request_id}`. The `approve` handler
> takes no request body / `approver` field; `reject` takes only an optional
> `?reason` query param. **Update (2026-06-06):** `require_different_user` is now
> enforced (unconditionally, the default) — `approve_command` rejects with 403
> "Cannot approve your own command" when the admin's `subject_id` equals the
> request's `submitter_id`. `reject` is unchanged (self-reject is benign).

### Browser WS Protocol

New browser-facing WS message types:

```json
// Sent to all connected browsers when a command is held
{"type": "approval_pending", "request_id": "...", "command": "rm -rf /", "policy": "dest-001", "expires_at": 1234567890.0}

// Sent to all connected browsers when resolved
{"type": "approval_resolved", "request_id": "...", "outcome": "approved", "approver": "alice"}
```

An admin browser with the correct role can approve/reject directly via the WS:

```json
{"type": "approval_decide", "request_id": "...", "decision": "approve", "note": "..."}
```

### Integration with `_handle_input`

In `browser_handlers.py`, `_handle_input` is updated:

```python
async def _handle_input(hub, ws, worker_id, msg_b):
    can_send = await hub.prepare_browser_input(worker_id, ws)
    if not can_send:
        return
    data = msg_b.get("data", "")
    if not data:
        return
    if len(data) > hub.max_input_chars:
        await ws.send_text(json.dumps({"type": "error", "message": "Input too long."}, ensure_ascii=True))
        return
    if hub._approval_gate is not None:
        ctx = ApprovalContext(principal=_get_principal(hub, ws, worker_id), role=_get_role(hub, ws, worker_id))
        forward = await hub._approval_gate.intercept(worker_id, data, principal=ctx.principal, role=ctx.role, hub=hub)
        if not forward:
            return   # held or rejected — gate handles worker control and browser notification
    ok = await hub.send_worker(worker_id, {"type": "input", "data": data, "ts": time.time()})
    ...
```

> **As-built (2026-06):** `browser_handlers.py` uses `gate = hub._policy_gate`
> (there is no `hub._approval_gate`) and calls
> `decision = await gate.intercept_input(data, context)` returning a
> `PolicyDecision` (not a bool); `context` is a `PolicyContext`, not an
> `ApprovalContext`. The `_is_noop_policy_gate(gate)` check short-circuits the
> zero-overhead path before any context is built. On `decision.action ==
> "hold"` the handler constructs the `ApprovalRequest` and pauses the browser.

### TermHub Constructor

```python
hub = TermHub(
    ...
    approval_gate=CommandApprovalGate(
        policies=[
            ApprovalPolicy(
                policy_id="dest-001",
                name="Destructive command gate",
                pattern=r"rm\s+-[a-z]*r[a-z]*f\s+/",
                min_role="operator",
                approver_url="https://hooks.slack.com/...",
                timeout_s=120,
                default_action="reject",
            )
        ],
        sink=HubEventApprovalSink(hub),
    ),
)
```

> **As-built (2026-06):** `TermHub.__init__`
> (`server/bridge/hub/core_impl.py`) takes a `policy_gate: PolicyGate | None`
> parameter (not `approval_gate`), defaulting to `NoOpPolicyGate()`. There is
> no `CommandApprovalGate`, no `policies=[...]` regex list, and no
> `HubEventApprovalSink`. To delegate approvals, pass a `WebhookPolicyGate`.

### Approval State Store

Pending requests are held in an in-memory `dict[str, ApprovalRequest]` with a TTL-based cleanup task (same pattern as `InMemoryResumeStore`). A `SqliteApprovalStore` is provided for the CF backend and for deployments requiring durability across restarts.

> **As-built (2026-06):** the hub's command-approval state lives in
> `InMemoryApprovalStore` (`server/bridge/hub/approvals.py`), wired
> unconditionally in `TermHub.__init__`; it is **not** durable (the durability
> ADR lists `approvals` as process-local). A `SqliteApprovalStore` does exist
> (`provide-uterm/.../control/plane/sqlite/approval_store.py`) but it is the
> distinct **control-plane** approval-*record* store (persisting
> `ApprovalRecord`) and is not wired into the command-approval gate here.

---

## CF Backend Parity

The CF DO backend stores pending approvals in SQLite. The `RuntimeProtocol` gains:

```python
async def intercept_input(self, data: str, *, ws: CFWebSocket) -> bool: ...
```

CF approval callbacks hit the DO's HTTP fetch handler at `POST /api/approvals/{request_id}/approve`.

> **As-built (2026-06):** the CF `RuntimeProtocol` (`cloudflare/contracts.py`)
> gained no `intercept_input` method — this CF-side approval-gate parity was
> not implemented.

---

## Security Considerations

- The `command` field in `ApprovalRequest` is stored and transmitted to approvers. Deployments must treat this as sensitive data (may contain credentials the operator is about to send).
- `require_different_user=True` is the default and must not be bypassable via the WS `approval_decide` path (enforce server-side, not just client-side).
- Timeout with `default_action="reject"` is the safe default. `default_action="approve"` is available for low-risk policies where availability matters more than control.
- The approval webhook URL must use HTTPS. The gate validates the URL scheme at startup.
- Approval decisions are idempotent — a double-approve is a 409 Conflict, not a double-forward.

---

## Testing

- `test_approval_gate_no_match.py` — non-matching commands pass through with zero overhead.
- `test_approval_gate_approve.py` — matching command is held, approved via REST, forwarded to worker.
- `test_approval_gate_reject.py` — matching command is held, rejected, worker receives resume but no input.
- `test_approval_gate_timeout.py` — held command times out, `default_action` applied.
- `test_approval_gate_same_user_rejected.py` — `require_different_user=True` blocks self-approval.
- `test_approval_gate_service_unreachable.py` — webhook fails, `default_action` applied, circuit breaker triggered.
- `test_approval_browser_notify.py` — `approval_pending` and `approval_resolved` messages reach all connected browsers.

---

## Open Questions

1. Should pending approvals survive a worker disconnect and re-attach on reconnect, or be auto-cancelled?
2. Should the held command be shown to the operator's browser in a "waiting for approval" UI state, or obscured?
3. Should approvers be resolvable from the session's `Principal` registry, or externally (LDAP, Slack users)?
4. Should there be a maximum number of concurrent pending approvals per session (to prevent DoS via approval queue flooding)?
5. Is there a "self-approval" escape hatch for break-glass situations, and how is it audited?
