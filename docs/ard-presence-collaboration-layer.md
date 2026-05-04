# ARD: Presence & Collaboration Layer

## Problem

Multiple browsers can observe the same terminal session simultaneously, but they are invisible to each other. There is no awareness of who else is watching, no way to communicate without leaving the terminal, and no mechanism to annotate what is happening on screen — crucial for incident response, pair programming on production, and training scenarios where a senior operator needs to guide a junior one.

provide-uterm already broadcasts terminal output to all connected browsers and tracks per-browser roles. The infrastructure for a real-time collaboration layer is already in place. What is missing is the presence model, the annotation data model, and the broadcast mechanism for non-terminal events.

---

## Goals

- Show all currently connected browsers (name, role, cursor position) to each other in real time.
- Allow any browser to place a named, colored annotation anchored to a region of the current terminal snapshot.
- Broadcast annotation create/update/clear events to all connected browsers instantly.
- Allow browsers to send ephemeral text messages (chat) within the session context.
- Require no changes to workers, the remote host, or the terminal transport.
- Impose zero overhead on sessions with a single browser (the common case).

---

## Non-Goals

- Persistent chat history stored server-side (chat is ephemeral, cleared on all-browser disconnect).
- Video or audio communication.
- Collaborative *editing* of terminal input (fan-out is covered in the separate ARD).
- Cross-session presence (presence is scoped to one worker session).

---

## Architecture

### Presence Model

```python
@dataclass(slots=True)
class BrowserPresence:
    browser_id: str        # stable per-connection identifier (uuid4 assigned at hello)
    principal: str         # subject_id (anonymous fallback: "anon:<browser_id[:8]>")
    display_name: str      # resolved from principal or "Anonymous"
    role: str              # "viewer" | "operator" | "admin"
    color: str             # deterministic HSL color derived from browser_id
    cursor_row: int        # 0-indexed row in current snapshot grid (-1 if not set)
    cursor_col: int        # 0-indexed column
    last_active_at: float  # time.time() of last WS message
    joined_at: float
```

`browser_id` is generated at hello-time and included in the `hello` message payload. It is stable for the lifetime of the WS connection (lost on reconnect, new ID assigned; resume flow restores the same ID from the token if `wall_created_at` check passes).

### Annotation Model

```python
@dataclass(slots=True)
class TerminalAnnotation:
    annotation_id: str     # uuid4
    browser_id: str        # author
    principal: str
    color: str
    row: int               # anchor row in snapshot grid
    col: int               # anchor column
    end_row: int           # end of highlighted region (-1 if point annotation)
    end_col: int
    text: str              # annotation label (max 280 chars)
    created_at: float
    updated_at: float
    kind: str              # "highlight" | "pointer" | "note"
```

Annotations are anchored to `(row, col)` in the snapshot grid. When the snapshot changes, annotations remain at their original coordinates — the browser reconciles whether they still make visual sense. A `snapshot_seq` field allows the browser to detect stale anchors.

Annotations are held in an in-memory `dict[str, TerminalAnnotation]` per session, cleared when all browsers disconnect (same lifecycle as the `WorkerTermState`).

### Chat Model

```python
@dataclass(slots=True)
class ChatMessage:
    message_id: str
    browser_id: str
    principal: str
    display_name: str
    text: str              # max 1000 chars
    ts: float
```

Chat messages are not stored server-side. They are broadcast and immediately forgotten. Each connected browser buffers its own history in memory.

### Browser WS Protocol Extensions

All new frame types are opt-in. Browsers that do not send presence frames are still visible to others (with `cursor_row=-1, cursor_col=-1`).

```json
// Browser → hub: cursor position update (high frequency, rate-limited to 10/s per browser)
{"type": "presence_cursor", "row": 12, "col": 34}

// Hub → all browsers: presence state snapshot (sent on connect + on any change)
{"type": "presence_state", "browsers": [
    {"browser_id": "...", "display_name": "alice", "role": "admin", "color": "#4a90d9", "cursor_row": 12, "cursor_col": 34, "last_active_at": 1234567890.0}
]}

// Browser → hub: create or update annotation
{"type": "annotation_set", "annotation_id": "...", "row": 5, "col": 0, "end_row": 7, "end_col": 80, "text": "This is the failing line", "kind": "highlight"}

// Hub → all browsers: annotation created/updated
{"type": "annotation_update", "annotation": {...}}

// Browser → hub: delete annotation (own only, or admin may delete any)
{"type": "annotation_clear", "annotation_id": "..."}

// Hub → all browsers: annotation removed
{"type": "annotation_removed", "annotation_id": "..."}

// Browser → hub: chat message
{"type": "chat_send", "text": "Let me check the /var/log/auth.log"}

// Hub → all browsers: chat message broadcast
{"type": "chat_message", "message_id": "...", "browser_id": "...", "display_name": "alice", "role": "admin", "text": "...", "ts": 1234567890.0}
```

### Rate Limiting

`presence_cursor` frames are rate-limited to 10 per second per browser (existing `TokenBucket` infrastructure). Exceeding the limit silently drops frames — no error response.

`annotation_set` and `chat_send` frames are rate-limited to 2 per second per browser to prevent collaboration spam.

### TermHub Integration

`WorkerTermState` gains:

```python
# Added to WorkerTermState dataclass
presences: dict[str, BrowserPresence]        # browser_id → presence
annotations: dict[str, TerminalAnnotation]   # annotation_id → annotation
```

`browser_id` is added to the `hello` message:

```json
{"type": "hello", ..., "browser_id": "a3f9c1d2", "presence_supported": true}
```

A new `broadcast_presence_state(worker_id)` method on `TermHub` sends the current presence snapshot to all connected browsers. It is called on:
- New browser connect
- Browser disconnect
- `presence_cursor` update (debounced: max 1 broadcast per 50 ms per session)

### `handle_browser_message` Extensions

In `browser_handlers.py`, three new `elif mtype ==` branches:

```python
elif mtype == "presence_cursor":
    await _handle_presence_cursor(hub, ws, worker_id, msg_b)

elif mtype in {"annotation_set", "annotation_clear"}:
    await _handle_annotation(hub, ws, worker_id, msg_b)

elif mtype == "chat_send":
    await _handle_chat(hub, ws, worker_id, msg_b)
```

### `_handle_presence_cursor`

Updates `st.presences[browser_id].cursor_row/col` under `hub._lock` and schedules a debounced `broadcast_presence_state` call.

### `_handle_annotation`

For `annotation_set`:
- Validates `row`, `col`, `text` (length ≤ 280).
- Rejects if `browser_id` already has ≥ 20 annotations (per-browser cap).
- Writes to `st.annotations` under `hub._lock`.
- Broadcasts `annotation_update` to all browsers.

For `annotation_clear`:
- Requires `annotation.browser_id == browser_id` OR `role == "admin"`.
- Removes from `st.annotations` under `hub._lock`.
- Broadcasts `annotation_removed`.

### `_handle_chat`

- Validates `text` (length ≤ 1000, non-empty after strip).
- Resolves `display_name` from presence state.
- Broadcasts `chat_message` to all connected browsers.
- Calls `hub.append_event(worker_id, "chat_message", {"from": principal, "text": text[:120]})` for the audit trail.

### Resume Integration

When a browser resumes (from `_handle_resume`), the resumed hello includes `browser_id` (restored from the resume token). The browser's presence entry is updated in-place rather than creating a new entry, preserving continuity for other browsers who observed the reconnect.

---

## CF Backend Parity

The CF DO backend stores presences and annotations in memory (within the DO — already single-writer, no lock needed). The `RuntimeProtocol` gains:

```python
async def broadcast_presence_state(self) -> None: ...
async def broadcast_annotation_update(self, annotation: dict) -> None: ...
async def broadcast_chat_message(self, message: dict) -> None: ...
```

Annotations are persisted in DO SQLite for the session lifetime (cleared on all-browser disconnect via a cleanup trigger).

---

## MCP Integration

Two new MCP tools:

- `session_annotations_list` — return all active annotations for a session
- `session_chat_send` — send a chat message as the MCP agent principal

This allows an AI agent to leave visible notes on a terminal session that all connected browsers can see, without disrupting the terminal stream.

---

## Security Considerations

- `display_name` is derived from `principal.subject_id`, not from any client-supplied field. Clients cannot impersonate other users via `presence_cursor`.
- `annotation_clear` for annotations authored by another user requires admin role; enforced server-side.
- Chat messages are not stored. They are not replayed to newly joining browsers (to avoid retroactive context leakage to observers who joined after a sensitive discussion).
- Presence state includes role but not capabilities — viewers cannot infer whether an admin is currently holding the hijack lease from presence data alone.
- `browser_id` in presence state is opaque (uuid4 hex). It does not encode or leak `subject_id`.
- Rate limits on presence and annotation frames prevent a malicious browser from flooding all observers with presence broadcasts.

---

## Testing

- `test_presence_on_connect.py` — joining browser receives `presence_state` with all current browsers.
- `test_presence_on_disconnect.py` — remaining browsers receive updated `presence_state` when one disconnects.
- `test_presence_cursor_broadcast.py` — cursor update from one browser reaches all others within 50 ms debounce window.
- `test_annotation_create_broadcast.py` — annotation created by one browser appears in `annotation_update` to all others.
- `test_annotation_clear_authz.py` — viewer cannot clear admin's annotation; admin can clear any.
- `test_chat_broadcast.py` — chat message from one browser reaches all connected browsers with correct metadata.
- `test_presence_resume.py` — resumed browser retains same `browser_id`; presence entry is updated, not duplicated.
- `test_presence_rate_limit.py` — 100 `presence_cursor` frames from one browser: ≤ 10 broadcasts reach others.

---

## Open Questions

1. Should annotations survive a snapshot change (coordinates may be stale)? Or should they auto-clear when the snapshot changes?
2. Should `display_name` be user-configurable (a nickname) or strictly derived from `principal.subject_id`?
3. Should chat history be replayed to browsers that join mid-session (last N messages), or strictly ephemeral?
4. Should there be a "pointer" annotation type that tracks the cursor position of a specific browser as an always-on overlay?
5. Should the MCP agent's chat messages be visually distinguished from human messages in the browser UI?
6. Is 20 annotations per browser the right cap, or should the cap be per-session (across all browsers)?
