# ARD: Session Audit & Compliance Recording

## Problem

Terminal sessions in privileged environments (production infrastructure, PCI-scoped systems, regulated industries) generate no structured audit trail. Raw byte dumps exist in some systems, but they are unsearchable, unverifiable, and useless for compliance workflows. There is no standard mechanism to prove *who* typed *what* at *when*, in a court-admissible or audit-ready form.

provide-uterm sits at the exact proxy layer where all terminal I/O passes. It is the right place to capture this data — without requiring changes to the remote host, the worker, or the terminal client.

---

## Goals

- Capture all terminal I/O (browser input, worker output) as a structured, time-indexed event stream.
- Produce tamper-evident logs suitable for compliance frameworks (SOC 2, PCI-DSS, HIPAA, ISO 27001).
- Support search and replay of any session by session ID, user (principal), time range, or matched content.
- Export recordings in standard formats (asciinema v2, JSONL, signed audit bundles).
- Impose negligible latency on the live terminal path (< 1 ms per event on the hot path).
- Integrate with existing `SessionRegistry`, `TermHub`, and principal identity infrastructure.

---

## Non-Goals

- Real-time content inspection or blocking (see ARD: Command Approval Workflows).
- Video-style screen capture (pixel-level recording).
- Replacing an external SIEM — this is structured event emission, not log aggregation.

---

## Architecture

### Recording Store Interface

```python
class RecordingStore(Protocol):
    def write_event(self, session_id: str, event: RecordingEvent) -> None: ...
    def get_events(self, session_id: str, *, after_seq: int = 0) -> list[RecordingEvent]: ...
    def finalize(self, session_id: str) -> RecordingMeta: ...
    def get_meta(self, session_id: str) -> RecordingMeta | None: ...
```

Implementations: `InMemoryRecordingStore` (tests), `FileRecordingStore` (JSONL on disk), `S3RecordingStore` (pluggable via boto3 or httpx), `SqliteRecordingStore` (embedded, no deps).

> **As-built (2026-06, updated 2026-07):** the shipped `RecordingStore` Protocol
> (`provide-uterm/src/provide/uterm/recording.py`) is **async** with the
> methods `start_session(session_id, metadata)`,
> `append_events(session_id, events)`, `end_session(session_id)`,
> `recording_meta(session_id)`, `get_entries(session_id, ...)` and
> `get_path(session_id)` — not the sync `write_event`/`get_events`/`finalize`/
> `get_meta` shown here. The implementations are `LocalFileRecordingStore`
> (JSONL on disk; the `FileRecordingStore` name above is not used),
> `InMemoryRecordingStore`, `NullRecordingStore`, and
> `WebhookRecordingStore` (`server/recording.py`). The proposed
> `S3RecordingStore` and `SqliteRecordingStore` were never built.
>
> **Multi-language ports:** Go (`packages/provide-uterm-go/recording`) and C#
> (`Provide.Uterm.Recording`) implement the same store contract. Query parity
> includes `limit=0` → default 200, clamp 1..500, and negative `offset` → skip
> nothing. See [recording-store-parity.md](./operations/recording-store-parity.md)
> for method maps, mermaid diagrams, and the asciinema demo matrix.

### Event Schema

```python
@dataclass(slots=True)
class RecordingEvent:
    seq: int               # monotonic per-session sequence number
    ts: float              # wall-clock time.time()
    kind: str              # "input" | "output" | "connect" | "disconnect" | "hijack" | "meta"
    principal: str | None  # subject_id from Principal, None for worker output
    role: str | None       # "viewer" | "operator" | "admin" | "worker"
    data: str              # raw bytes (base64) or structured JSON for meta events
    hmac: str              # HMAC-SHA256(seq|ts|kind|data, signing_key) for tamper evidence
```

> **As-built (2026-06):** recording events are plain dicts shaped
> `{ts, event, data, session_id}` (`recording.py`), with no
> `seq`/`kind`/`principal`/`role`/`hmac` fields. The per-event HMAC tamper
> scheme was not built into recordings (see Tamper Evidence below).

### Hot Path Integration

Recording hooks are registered on `TermHub` at construction time:

```python
hub = TermHub(
    ...
    recording_store=FileRecordingStore("/var/log/uterm/"),
    recording_signing_key=config.audit.signing_key,
)
```

> **As-built (2026-06):** recording is **not** wired through the `TermHub`
> constructor — `TermHub.__init__` (`server/bridge/hub/core_impl.py`) has no
> `recording_store` or `recording_signing_key` parameter. Instead the
> `recording_store` is passed to the `SessionRegistry`/session runtime
> (`server/registry.py`, `server/runtime.py`), and the concrete store is
> selected in `app/factory_impl.py` (`WebhookRecordingStore` /
> `InMemoryRecordingStore` / `NullRecordingStore` / `LocalFileRecordingStore`
> per `RecordingConfig`). No `recording_signing_key` is plumbed anywhere.

Two hook points in the existing WS pipeline:

1. **Worker → browser** (`ws_worker_term` inner loop): after `hub.broadcast(worker_id, frame)`, call `store.write_event(session_id, output_event)` in a fire-and-forget `asyncio.create_task`.
2. **Browser → worker** (input handler in `browser_handlers.py`): after `hub.send_worker(...)`, call `store.write_event(session_id, input_event)`.

Both calls are non-blocking (task-based). Store implementations must be thread-safe but may buffer internally for batch writes.

### Tamper Evidence

Each event is HMAC-SHA256 signed over `f"{seq}:{ts:.6f}:{kind}:{data}"` using a per-deployment signing key. A separate `verify_recording(session_id, store, signing_key)` utility checks the full chain and reports the first broken link.

> **As-built (2026-06):** the per-event HMAC scheme and `verify_recording`
> utility were not built — recordings carry no signatures. Tamper-evidence
> shipped instead as a separate **sha256 hash-chain** WORM audit log
> (`server/audit_chain.py`: `AuditChain`, `GENESIS_HASH`, `verify_records`,
> `verify_audit_log`), verified via the `uterm audit verify <path>` CLI. This
> is a distinct subsystem from the recording store, not a property of
> individual recording events.

### Replay API

```
GET /api/sessions/{session_id}/recording
    → RecordingMeta (duration, event_count, size_bytes, finalized_at)

GET /api/sessions/{session_id}/recording/events?after_seq=0&limit=1000
    → list[RecordingEvent]

GET /api/sessions/{session_id}/recording/export?format=asciinema
    → streaming asciinema v2 JSONL
```

The asciinema export converts output events to `[delay, "o", data]` frames and is playable in standard asciinema player or `asciinema play`.

> **As-built (2026-06):** the shipped replay routes (`server/routes/sessions.py`)
> are `GET /api/sessions/{id}/recording` (meta),
> `GET /api/sessions/{id}/recording/entries` (paginated entries) and
> `GET /api/sessions/{id}/recording/download` (full JSONL). There is no
> `/recording/events` route and no `/recording/export` route. asciinema export
> was **not** implemented anywhere in the codebase. The Cloudflare backend
> mirrors the same `/recording`, `/recording/entries`, `/recording/download`
> trio (`cloudflare/.../http_routes/_recording.py`).

### Retention & Lifecycle

`RecordingMeta` tracks `started_at`, `finalized_at`, `size_bytes`, `event_count`. Finalization happens on session disconnect or explicit `DELETE /api/sessions/{id}`. A background `retention_days` policy can auto-expire old recordings.

---

## CF Backend Parity

The Cloudflare DO backend writes events to a `recordings` SQLite table alongside the existing `resume_tokens` table. The `RuntimeProtocol` gains:

```python
def record_event(self, kind: str, data: str, *, principal: str | None = None) -> None: ...
```

Export endpoints are added to `http_routes.py`.

> **As-built (2026-06):** no dedicated `recordings` table was added — the CF
> backend exposes the pre-existing `session_events` table
> (`cloudflare/state/store.py`) through recording-compatible routes in
> `cloudflare/.../http_routes/_recording.py` (`/recording`,
> `/recording/entries`, `/recording/download`). `RuntimeProtocol`
> (`cloudflare/contracts.py`) gained no `record_event` method, and no
> asciinema export endpoints were added.

---

## Security Considerations

- Signing key must not be stored in the recording itself.
- `principal` field uses `subject_id` from the resolved `Principal`, never a raw header value.
- Recordings containing credentials (accidentally typed passwords) are flagged via a configurable regex scanner at finalization time — a warning is appended to `RecordingMeta.warnings`, the bytes are not redacted (audit completeness), but the flag triggers an alert hook.
- Access to replay endpoints is gated by `authz.can_read_session()` — same as live session access.

---

## Testing

- `InMemoryRecordingStore` for all unit tests (no I/O).
- `test_recording_hot_path.py` — verify events are emitted without blocking, correct seq ordering, correct principal tagging.
- `test_recording_tamper_evidence.py` — verify HMAC chain validates on clean recording, fails on any mutation.
- `test_recording_export_asciinema.py` — compare export output against known-good fixture.
- `test_recording_retention.py` — verify finalization and TTL behavior.

---

## Open Questions

1. Should `role="viewer"` browser connections be recorded? (They receive output but send no input.)
2. Should the signing key be per-session (derived from session_id + master key) or global?
3. What is the maximum recording size before the store must roll or reject new events?
4. Should replay be gated by a separate `can_replay_session` capability distinct from `can_read_session`?
