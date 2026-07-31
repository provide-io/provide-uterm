# ARD: Multi-Session Fan-Out (Broadcast Input)

## Problem

Coordinated operations across a fleet of servers — rolling deployments, parallel configuration changes, synchronized maintenance windows — require the same commands to be executed on N terminal sessions simultaneously. Current tooling (cssh, tmux broadcast, Ansible) operates at the SSH/host layer with no visibility into the proxy, no role-based access control, and no per-session response aggregation.

provide-uterm controls the input path for every managed session. A fan-out primitive at the hub layer can broadcast a single input stream to N sessions with full RBAC, audit trail, per-session output capture, and live divergence detection — without any host-side agent or SSH multiplexer.

---

## Goals

- Send a single input event to a named set of target sessions simultaneously.
- Capture per-session output for a configurable window after each fanned-out input.
- Detect and surface divergence: sessions whose output differs significantly from the majority.
- Support dynamic fan-out groups (ad-hoc sets) and persistent named groups.
- Gate every fan-out operation on an authenticated global administrator and
  reauthorize every target session at send time.
- Emit a structured `FanOutEvent` to the audit trail for each command sent and each response captured.
- Impose no additional latency on non-fan-out sessions.

---

## Non-Goals

- Replacing Ansible, Terraform, or configuration management tools. Fan-out is for *interactive* parallel terminal control, not automated orchestration.
- Coordinating across multiple provide-uterm deployments (single-hub scope only for v1).
- Modifying output on a per-session basis before delivery to the controlling browser.

---

## Architecture

### Group Model

```python
@dataclass(slots=True)
class FanOutGroup:
    group_id: str
    name: str
    worker_ids: list[str]          # target sessions
    created_by: str                # principal subject_id
    created_at: float
    mode: str                      # "parallel" | "sequential"
    stop_on_first_error: bool      # sequential only: halt if a session returns non-zero exit code
    response_window_ms: int        # how long to collect output after each send (default 2000)
    divergence_threshold: float    # Levenshtein ratio below which outputs are flagged as divergent (0.0–1.0)
```

Groups are ephemeral by default (in-memory, discarded on hub restart). A `SqliteFanOutStore` provides persistence.

> **As-built (2026-06):** the shipped `FanOutGroup`
> (`server/bridge/fanout/_models.py`) uses `quiesce_ms`/`max_response_ms`
> (not `response_window_ms`) and adds a `grants: list[str]` field for shared
> access. `divergence_threshold` is a `difflib.SequenceMatcher` ratio, not a
> Levenshtein ratio. Persistence was **not** built: the only store is the
> `FanOutStore` Protocol plus the in-memory `InMemoryFanOutStore`
> (`server/bridge/fanout/_store.py`); there is no `SqliteFanOutStore`. Groups
> are in-memory only.

### Fan-Out Controller

```python
class FanOutController:
    def __init__(self, hub: TermHub, store: FanOutStore | None = None) -> None: ...

    async def create_group(self, group: FanOutGroup) -> str: ...          # returns group_id
    async def delete_group(self, group_id: str) -> None: ...
    async def get_group(self, group_id: str) -> FanOutGroup | None: ...
    async def list_groups(self, principal: str) -> list[FanOutGroup]: ...

    async def send(
        self,
        group_id: str,
        data: str,
        *,
        principal: str,
        response_window_ms: int | None = None,
    ) -> FanOutResult: ...
```

`send()` fans the input to all target sessions via `hub.send_worker(worker_id, ...)` in parallel (using `asyncio.gather`). It then collects snapshot deltas from each session for `response_window_ms` milliseconds, assembles a `FanOutResult`, and emits audit events.

### Result Schema

```python
@dataclass(slots=True)
class SessionFanOutResult:
    worker_id: str
    ok: bool                    # False if worker not connected
    output_delta: str | None    # output captured after the send
    divergent: bool             # True if output differs significantly from majority

@dataclass(slots=True)
class FanOutResult:
    group_id: str
    send_id: str                # uuid4 per send operation
    command: str
    sent_at: float
    results: list[SessionFanOutResult]
    divergent_sessions: list[str]    # worker_ids flagged as divergent
    failed_sessions: list[str]       # worker_ids where ok=False
```

### Output Delta Collection

After sending input, the controller subscribes each target session to a temporary `OutputCollector` that captures snapshot diffs for `response_window_ms`. A snapshot diff is the set of lines added since the last snapshot. The collector is removed after the window closes.

Divergence is detected by computing pairwise Levenshtein similarity between all session output deltas and comparing against `group.divergence_threshold`. Sessions below the threshold are added to `divergent_sessions`.

> **As-built (2026-06):** `OutputCollector`
> (`server/bridge/fanout/_collector.py`) subscribes to the EventBus `term`
> and `snapshot` events for a session and concatenates `term`-event deltas
> (falling back to the last `snapshot.screen` for snapshot-only connectors) —
> it does not diff snapshot lines. Collection is bounded by an adaptive
> `quiesce_ms` (silence window) plus a hard `max_response_ms` cap, not a fixed
> `response_window_ms`. Divergence
> (`server/bridge/fanout/_divergence.py:compute_divergence`) uses
> `difflib.SequenceMatcher(None, a, b).ratio()` (gestalt pattern matching),
> **not** Levenshtein, and compares each output against the single "majority"
> output (highest average similarity to all others) rather than all pairwise.

### REST API

```
POST /api/fanout/groups
    Body: FanOutGroupCreate
    → FanOutGroup

GET /api/fanout/groups
    → list[FanOutGroup]

DELETE /api/fanout/groups/{group_id}
    → 204 No Content

POST /api/fanout/groups/{group_id}/send
    Body: {"data": "kubectl rollout status ...", "response_window_ms": 3000}
    → FanOutResult

POST /api/fanout/groups/{group_id}/grants
    Body: {"grantee": "alice"}
    → 204 No Content
```

Create, list, delete, send, and grant all require an authenticated **global
administrator**. A session-scoped admin is not a global administrator and is
rejected before request parsing or group lookup. `created_by` is set from the
resolved `Principal`.

> **As-built (2026-06):** the shipped routes
> (`server/bridge/fanout/_routes.py`) are `POST/GET /api/fanout/groups`,
> `DELETE /api/fanout/groups/{group_id}`,
> `POST /api/fanout/groups/{group_id}/send`, and
> `POST /api/fanout/groups/{group_id}/grants` (grant a principal access to a
> group). There is no `/history` route. `POST groups` returns
> `{group_id, name, session_count}` and `GET groups` returns
> `[{group_id, name, session_count, mode}]` — not full `FanOutGroup` objects.
> The send body carries `quiesce_ms`/`max_response_ms`, not
> `response_window_ms`.

### Browser WS Protocol

A controlling global-admin browser can initiate fan-out via WS (in addition to REST):

```json
// Browser → hub
{"type": "fanout_send", "group_id": "fleet-prod", "data": "uptime\n"}

// Hub → controlling browser (streamed as results arrive)
{"type": "fanout_result", "send_id": "...", "results": [...], "divergent_sessions": [...]}

// Hub → all browsers watching a target session (so observers know input came from fan-out)
{"type": "fanout_input", "group_id": "fleet-prod", "send_id": "...", "command": "uptime\n", "from_principal": "alice"}
```

> **As-built (2026-06):** the `fanout_send` → `fanout_result` exchange shipped
> (WS handler `bridge/routes/websockets_impl.py`, reply to the initiating
> browser only). **Update (2026-06-06):** the `fanout_input` observer-broadcast
> frame is now built — `FanOutController._notify_fanout_observers` broadcasts
> `{type: fanout_input, group_id, send_id, command, from_principal}` to each
> target session's observers on every send (parallel + sequential + the
> approved-release path), so viewers can distinguish fan-out input from a
> local hijack.

### Sequential Mode

When `mode="sequential"`, the controller iterates sessions in `group.worker_ids` order, awaiting the response window before proceeding to the next. If `stop_on_first_error=True`, it halts when the output delta from a session does not match the expected success pattern (configurable regex, defaults to absence of known error strings).

### TermHub Integration

`TermHub` gains an optional `fan_out_controller` attribute set after construction (same pattern as `on_worker_empty`):

```python
hub = TermHub(...)
controller = FanOutController(hub=hub, store=SqliteFanOutStore("/var/lib/uterm/fanout.db"))
hub.fan_out_controller = controller
```

No changes to `TermHub.__init__` signature. The REST and WS routes check `hub.fan_out_controller is not None` before handling fan-out frames.

> **As-built (2026-06):** the `hub.fan_out_controller`-attribute pattern
> shipped as designed, but `SqliteFanOutStore` does not exist —
> `FanOutController` defaults to `InMemoryFanOutStore()` when no `store=` is
> passed (`server/bridge/fanout/_controller.py`), and that is what the factory
> wires (`app/factory_impl.py`). The constructor's `store` argument is
> keyword-only and accepts any `FanOutStore` Protocol implementation.

---

## CF Backend Parity

The Cloudflare backend does **not** currently serve fan-out. A future port
could operate at the KV + DO level: the Default worker would fan the send
request to each session's DO instance via HTTP fetch, with response aggregation
through each DO's snapshot surface. That is an unimplemented design, not an
advertised capability.

If implemented, it need not add methods to `RuntimeProtocol`; orchestration can
remain at the Default worker layer.

---

## MCP Integration

Two new MCP tools:

- `fanout_group_create` — create a named group of sessions
- `fanout_send` — send input to a group, return aggregated results

This enables AI agents (Claude via MCP) to coordinate fleet-wide terminal operations with full result aggregation — a capability not available in any current MCP terminal tool.

The tools call the server REST surface and do not weaken its boundary:
creating a group and sending input both require an authenticated global
administrator at the server, regardless of any client-side tool visibility.

---

## Security Considerations

- Every fan-out operation — create, list, delete, send, and grant — requires an
  authenticated global administrator. Session-scoped admins are rejected.
- `created_by` is recorded on every group and every send operation and is non-spoofable (set from `Principal`, not from request body).
- Sessions in a group that do not have a connected worker are reported as `failed` but do not block the fan-out to other sessions.
- The `fanout_input` broadcast to observer browsers includes `from_principal` so viewers can see that input came from a fan-out operation, not from a local hijack.
- Groups may not contain sessions to which the creating principal does not have `can_read_session` access. This is enforced at group creation time by checking `authz.can_read_session(principal, session_def)` for each `worker_id`.
- Maximum group size is configurable (default 50 sessions) to prevent inadvertent fleet-wide destructive commands.

> **As-built (2026-07-31):** all five REST operations enforce the global-admin
> boundary before parsing or group lookup; browser-WS send and approval release
> enforce the same boundary. The non-spoofable `created_by` and configurable
> max group size shipped as described. The per-worker `can_read_session` check is enforced
> at group creation and again on every send and approval release. Unknown IDs
> are rejected by default; deployments that require create-then-connect must
> explicitly set `fanout_allow_unknown_members = true`. That option affects
> admission only: dormant, deleted, or revoked members receive no input or
> observer broadcast and are returned as failed. Group grants never substitute
> for session authorization. The `fanout_input` observer broadcast is also
> built (see the Browser WS Protocol note above).

---

## Testing

- `test_fanout_parallel_send.py` — fan-out to 5 sessions, all workers connected, all receive input, results aggregated correctly.
- `test_fanout_partial_failure.py` — 2 of 5 workers not connected; `failed_sessions` populated, others succeed.
- `test_fanout_sequential_stop_on_error.py` — sequential mode halts after first session output matches error pattern.
- `test_fanout_divergence_detection.py` — sessions with differing output are flagged as divergent.
- `test_fanout_authz.py` — non-global-admin principals cannot create groups;
  groups may not include sessions the principal cannot read.
- `test_fanout_mcp.py` — `fanout_send` MCP tool returns structured results consumable by an AI agent.

---

## Open Questions

1. Should fan-out groups be scoped to a principal (only creator can use) or shared (any admin can use)?
2. Should there be a "dry run" mode that shows which sessions would receive the input without sending it?
3. Should divergence detection use Levenshtein distance, semantic embedding similarity, or a configurable comparator?
4. What is the right response window default (2000 ms)? Should it adapt based on session type (Telnet vs SSH vs WS)?
5. Should sequential mode support a configurable inter-session delay to avoid thundering herd on shared infrastructure?
6. Should the MCP `fanout_send` tool stream partial results as sessions respond, or batch the full result?
