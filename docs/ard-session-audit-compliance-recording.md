# ARD: Session Audit & Compliance Recording

## Problem

Terminal sessions in privileged environments (production infrastructure, PCI-scoped systems, regulated industries) generate no structured audit trail. Raw byte dumps exist in some systems, but they are unsearchable, unverifiable, and useless for compliance workflows. There is no standard mechanism to prove *who* typed *what* at *when*, in a court-admissible or audit-ready form.

provide-terminal sits at the exact proxy layer where all terminal I/O passes. It is the right place to capture this data — without requiring changes to the remote host, the worker, or the terminal client.

______________________________________________________________________

## Goals

- Capture all terminal I/O (browser input, worker output) as a structured, time-indexed event stream.
- Produce tamper-evident logs suitable for compliance frameworks (SOC 2, PCI-DSS, HIPAA, ISO 27001).
- Support search and replay of any session by session ID, user (principal), time range, or matched content.
- Export recordings in standard formats (asciinema v2, JSONL, signed audit bundles).
- Impose negligible latency on the live terminal path (< 1 ms per event on the hot path).
- Integrate with existing `SessionRegistry`, `TermHub`, and principal identity infrastructure.

______________________________________________________________________

## Non-Goals

- Real-time content inspection or blocking (see ARD: Command Approval Workflows).
- Video-style screen capture (pixel-level recording).
- Replacing an external SIEM — this is structured event emission, not log aggregation.

______________________________________________________________________

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

### Hot Path Integration

Recording hooks are registered on `TermHub` at construction time:

```python
hub = TermHub(
    ...
    recording_store=FileRecordingStore("/var/log/uterm/"),
    recording_signing_key=config.audit.signing_key,
)
```

Two hook points in the existing WS pipeline:

1. **Worker → browser** (`ws_worker_term` inner loop): after `hub.broadcast(worker_id, frame)`, call `store.write_event(session_id, output_event)` in a fire-and-forget `asyncio.create_task`.
1. **Browser → worker** (input handler in `browser_handlers.py`): after `hub.send_worker(...)`, call `store.write_event(session_id, input_event)`.

Both calls are non-blocking (task-based). Store implementations must be thread-safe but may buffer internally for batch writes.

### Tamper Evidence

Each event is HMAC-SHA256 signed over `f"{seq}:{ts:.6f}:{kind}:{data}"` using a per-deployment signing key. A separate `verify_recording(session_id, store, signing_key)` utility checks the full chain and reports the first broken link.

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

### Retention & Lifecycle

`RecordingMeta` tracks `started_at`, `finalized_at`, `size_bytes`, `event_count`. Finalization happens on session disconnect or explicit `DELETE /api/sessions/{id}`. A background `retention_days` policy can auto-expire old recordings.

______________________________________________________________________

## CF Backend Parity

The Cloudflare DO backend writes events to a `recordings` SQLite table alongside the existing `resume_tokens` table. The `RuntimeProtocol` gains:

```python
def record_event(self, kind: str, data: str, *, principal: str | None = None) -> None: ...
```

Export endpoints are added to `http_routes.py`.

______________________________________________________________________

## Security Considerations

- Signing key must not be stored in the recording itself.
- `principal` field uses `subject_id` from the resolved `Principal`, never a raw header value.
- Recordings containing credentials (accidentally typed passwords) are flagged via a configurable regex scanner at finalization time — a warning is appended to `RecordingMeta.warnings`, the bytes are not redacted (audit completeness), but the flag triggers an alert hook.
- Access to replay endpoints is gated by `authz.can_read_session()` — same as live session access.

______________________________________________________________________

## Testing

- `InMemoryRecordingStore` for all unit tests (no I/O).
- `test_recording_hot_path.py` — verify events are emitted without blocking, correct seq ordering, correct principal tagging.
- `test_recording_tamper_evidence.py` — verify HMAC chain validates on clean recording, fails on any mutation.
- `test_recording_export_asciinema.py` — compare export output against known-good fixture.
- `test_recording_retention.py` — verify finalization and TTL behavior.

______________________________________________________________________

## Open Questions

1. Should `role="viewer"` browser connections be recorded? (They receive output but send no input.)
1. Should the signing key be per-session (derived from session_id + master key) or global?
1. What is the maximum recording size before the store must roll or reject new events?
1. Should replay be gated by a separate `can_replay_session` capability distinct from `can_read_session`?
