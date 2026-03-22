# Proposal: WebSocket Session Resumption

## Evaluation: uwarp-space Implementation (March 2026)

uwarp-space has **already implemented session resumption** in production, but it does **NOT use provide-terminal's WS infrastructure**. It has a complete parallel implementation:

| Aspect | uwarp-space (deployed) | provide-terminal (this proposal) |
|--------|------------------------|-------------------------------|
| **Architecture** | Custom DO + SQLite, no provide-terminal dependency | Library-level, pluggable store |
| **Storage** | DO SQLite `_meta` table | In-memory (FastAPI) or pluggable (Redis/DB/SQLite) |
| **Token in** | WS frame (first message) | WS frame (first message) — same |
| **TTL** | 3600s (1 hour) | 300s default (configurable) |
| **State preserved** | Player ID, game state, command loop, input buffer | Role, hijack ownership, hello state |
| **Resume anchor** | Player identity (login + password → player_id) | Browser session (role, hijack lease) |
| **Hibernation** | Yes (CF attachment API + SQLite) | N/A for FastAPI; CF path uses SQLite |
| **Testing** | Unit + E2E Playwright (653 lines) | Proposed |

**Key files in uwarp-space:**
- `packages/uwarp-worker/src/uwarp_worker/terminal_session.py` — token lifecycle (85 KB)
- `packages/uwarp-worker/src/uwarp_worker/ws_handlers.py` — resume message handling (371 lines)
- `packages/uwarp/src/uwarp/frontend/web/static/terminal.js` — browser-side resume (1400+ lines)
- `packages/uwarp-worker/src/uwarp_worker/env_settings.py` — config (RESUME_TOKEN_TTL_SECONDS)

**Conclusion:** uwarp's resume is game-specific and doesn't use provide-terminal. Adding resume to provide-terminal is still valuable for the library's own operator/admin dashboard use case and future consumers, but won't directly benefit uwarp unless uwarp adopts provide-terminal's WS infrastructure.

---

## Problem

When a browser WebSocket drops (CF idle timeout ~100s, network blip, mobile tab backgrounded), the browser reconnects as a brand-new anonymous session. All hijack ownership and UI state is lost. The player gets dumped to the welcome screen.

Server-side state IS preserved (worker output streams, snapshots, event logs, input mode), but the reconnecting browser can't prove it was the same session. There is no resumption path.

**Consumer impact (uwarp):** Players in-game lose all progress on WS drop. The DO has hibernation rehydration (same WS survives DO sleep via CF attachment API), but client reconnection (new WS) creates a new anonymous session.

---

## Decision: Protocol Extension, Not Separate "Meta" WS

A second "meta" WS was considered but rejected:
- Doubles connection count per browser
- Adds a second reconnect state machine with its own failure modes
- Complicates CF DO path (socket management via `ctx.getWebSockets()` and attachment serialization)
- The existing WS already dispatches on `msg.type` — adding `{"type":"resume"}` is minimal

The resume token is sent **inside the WS frame as the first message**, not in the URL query string.

---

## Design

### Token Flow

```
1. Browser connects
2. Server accepts, registers browser, sends hello (with resume_supported: true)
3. Server generates resume_token (secrets.token_urlsafe(32), 256-bit)
4. Hello message includes resume_token
5. Browser stores token in sessionStorage

--- WS drops ---

6. Browser reconnects to /ws/browser/{id}/term (NO token in URL)
7. Server accepts, sends initial hello (fresh session, resume_supported: true)
8. Browser immediately sends {"type":"resume","token":"xxx"} as first message
9. Server validates token:
   - Valid: revoke old token, re-register with stored role, reclaim hijack
           if lease alive, send updated hello with resumed: true + new token
   - Invalid/expired: ignore, initial hello stands (fresh session, no error)
```

### Security Properties

| Threat | Mitigation |
|--------|-----------|
| Token in URL/logs | Token sent inside encrypted WS frame only. Never in query string, access logs, browser history, CDN logs, or referrer headers |
| Replay attack | Token revoked on successful resume. New token issued. Old token invalid immediately |
| Token theft from server logs | WS frame content not logged by default. Token is opaque (no encoded identity) |
| Brute force | 256-bit token space (token_urlsafe(32)). Infeasible |
| MITM extraction | Requires TLS compromise. Same threat model as session cookies |

### What the Library Preserves on Resume

- Browser **role** (viewer/operator/admin)
- **Hijack ownership** (if dashboard lease hasn't expired and no one else took it)
- The correct **hello** state (worker_online, input_mode, last snapshot)

### What the Consumer Handles

- Token-to-identity mapping (optional — library tokens are opaque session handles)
- Application-level state restoration (game state, login state, player position)
- Token TTL policy (library default: 300s, configurable via `resume_ttl_s`)
- Token revocation on explicit logout/quit

### Opt-in Design

```python
# Disabled (default) — zero overhead, no tokens generated
hub = TermHub()

# Enabled with in-memory store (single-process FastAPI)
from provide.terminal.hijack.hub.resume import InMemoryResumeStore
hub = TermHub(resume_store=InMemoryResumeStore(), resume_ttl_s=300)

# Enabled with custom store (Redis, DB, etc.)
hub = TermHub(resume_store=MyRedisResumeStore(), resume_ttl_s=3600)
```

---

## API Surface

### New File: `src/provide/terminal/hijack/hub/resume.py` (~200 LOC)

```python
@dataclass
class ResumeSession:
    """Bookkeeping for one resumable browser session."""
    token: str
    worker_id: str
    role: str                  # viewer/operator/admin
    created_at: float          # time.monotonic()
    was_hijack_owner: bool     # True if this session held dashboard hijack
    ttl_s: float

class ResumeTokenStore(Protocol):
    """Pluggable token store. Library provides InMemoryResumeStore."""
    def create(self, worker_id: str, role: str, ttl_s: float) -> str
    def get(self, token: str) -> ResumeSession | None
    def mark_hijack_owner(self, token: str, is_owner: bool) -> None
    def revoke(self, token: str) -> None

class InMemoryResumeStore:
    """Default in-memory implementation. Suitable for single-process FastAPI.
    Lost on server restart — consumer can provide persistent store."""
    # dict[str, ResumeSession], auto-prunes expired on access
```

### Protocol Messages

**Hello (server → browser):**
```json
{
  "type": "hello",
  "worker_id": "myworker",
  "can_hijack": true,
  "hijacked": false,
  "worker_online": true,
  "input_mode": "hijack",
  "role": "admin",
  "resume_supported": true,
  "resume_token": "abc123...",
  "resumed": false
}
```

**Resume request (browser → server, first message after connect):**
```json
{
  "type": "resume",
  "token": "abc123..."
}
```

**Resume success (server → browser, replaces initial hello):**
```json
{
  "type": "hello",
  "worker_id": "myworker",
  "can_hijack": true,
  "hijacked": true,
  "hijacked_by_me": true,
  "worker_online": true,
  "input_mode": "hijack",
  "role": "admin",
  "resume_supported": true,
  "resume_token": "def456...",
  "resumed": true
}
```

### Consumer Hook

```python
# Optional callback for consumer-level resume validation
hub = TermHub(
    resume_store=InMemoryResumeStore(),
    on_resume=my_resume_validator,  # async (token, session) -> bool
)

async def my_resume_validator(token: str, session: ResumeSession) -> bool:
    """Return False to reject the resume (e.g., player banned, session expired)."""
    player = await db.get_player_by_session(token)
    return player is not None and not player.banned
```

The library treats resume tokens as opaque session handles. Without a
consumer-supplied `on_resume` policy, a successful FastAPI resume restores the
role cached in the token for that browser session.

---

## Files to Modify

### `src/provide/terminal/hijack/hub/core.py`
- Add constructor params: `resume_store: ResumeTokenStore | None = None`, `resume_ttl_s: float = 300`, `on_resume: Callable | None = None`
- Store as instance attributes

### `src/provide/terminal/hijack/hub/connections.py`
- `register_browser()`: if resume_store configured, create token, store `ws → token` mapping
- Return `resume_token` in the browser_state dict
- `cleanup_browser_disconnect()`: look up token for disconnecting ws, mark `was_hijack_owner` if applicable. Do NOT revoke token (needed for resume)
- Add `_ws_to_resume_token: dict[WebSocket, str]` tracking

### `src/provide/terminal/hijack/routes/websockets.py`
- After sending initial hello + hijack_state + snapshot:
  - Peek at first message from browser (with short timeout, e.g. 0.5s)
  - If `{"type":"resume","token":"..."}`: validate in store, call `on_resume` if set
  - If valid: revoke old token, re-register with stored role, reclaim hijack if applicable, send updated hello
  - If invalid or timeout: continue with existing fresh session
- Initial hello includes `"resume_supported": true` if store configured
- Resume hello includes `"resume_token": new_token, "resumed": true`

### `packages/provide-terminal-frontend/src/hijack.js`
- In hello handler: if `msg.resume_token`, store in `this._resumeToken` and `sessionStorage.setItem('uterm_resume_' + this._workerId, token)`
- In `ws.onopen`: if stored token exists, immediately send `{"type":"resume","token":"..."}` as first message
- In second hello handler (after resume): update stored token with new one
- On disconnect: do NOT clear `_resumeToken` (needed for reconnect)
- On explicit release/quit (if consumer signals): clear token from sessionStorage

### `packages/provide-terminal-cloudflare/src/provide_terminal_cloudflare/do/session_runtime.py`
- In `webSocketMessage()` for browser: detect `{"type":"resume","token":"..."}` as first message
- Validate against sqlite store
- If valid: update attachment with stored role, send updated hello with `resumed: true` + new token
- If invalid: ignore, existing hello stands

### `packages/provide-terminal-cloudflare/src/provide_terminal_cloudflare/state/store.py`
- Add `resume_tokens` table: `(token TEXT PK, worker_id TEXT, role TEXT, was_hijack_owner INT, created_at REAL, expires_at REAL)`
- Methods: `create_resume_token()`, `validate_resume_token()`, `revoke_resume_token()`, `mark_hijack_owner()`

---

## Edge Cases

| Case | Behavior |
|------|----------|
| Token expired | Ignored, fresh flow. No error sent |
| Wrong worker_id | Ignored, fresh flow |
| Two tabs same token | First resume wins, new token issued. Old token revoked. Second tab gets fresh flow |
| Token TTL > hijack lease TTL | Resume succeeds for role/snapshot but hijack not reclaimed (lease expired). Correct |
| Explicit quit | Consumer calls hub method to revoke token. Client clears sessionStorage |
| Server restart (FastAPI) | InMemoryResumeStore lost. Tokens invalid. Fresh flow. Consumer can use persistent store |
| CF DO restart | Sqlite survives. Tokens valid until TTL |
| WS drops during resume handshake | Old token already revoked, new token never received. Client has stale token → fresh flow on next reconnect |
| Worker broadcast before resume | Terminal output arrives before client sends resume. Snapshot re-sent on resume overwrites. No data loss |
| CF DO hibernation between connect and resume | Sqlite resume store survives hibernation. Attachment updated after validation. Works correctly |

---

## Open Questions

### Token grace period on revocation

Currently: old token revoked immediately on successful resume. New token issued.

**Risk:** If the WS drops again during the resume handshake (after server revokes old token but before client receives new token), the client has a stale token and can't resume.

**Possible fix:** Keep old token valid for a short grace period (10s) after issuing the new one. Or: don't revoke old token until client sends its first non-resume message (proving it received the new token).

**Recommendation:** Keep it simple — revoke immediately. The probability of a double-drop within the handshake window is negligible. If it happens, the user gets a fresh session (acceptable degradation).

### sessionStorage vs localStorage

`sessionStorage` dies when the tab closes. `localStorage` persists indefinitely.

| Storage | Survives tab close | Survives network blip | Stale token risk |
|---------|-------------------|----------------------|------------------|
| sessionStorage | No | Yes | Low (cleared on tab close) |
| localStorage | Yes | Yes | High (tokens accumulate) |

**Recommendation:** `sessionStorage` for the library default. The primary use case is network blip recovery, not tab restoration. Consumers who want tab-survive can override client storage via a config option.

### Consumer resume callback (on_resume)

Should this be sync or async? Async allows DB lookups but adds latency to the resume handshake. The consumer is already waiting for the hello.

**Recommendation:** Async. The consumer's validation might need a DB/KV lookup. The latency is acceptable — it's a one-time cost on reconnect, not on every message.

### Multiple workers per browser

Each worker gets its own token. Storage key includes worker_id: `uterm_resume_{worker_id}`. No conflict.

---

## Implementation Order

1. **`resume.py`** — ResumeSession, ResumeTokenStore protocol, InMemoryResumeStore
2. **`core.py`** — constructor params (resume_store, resume_ttl_s, on_resume)
3. **`connections.py`** — wire token creation/tracking into register/disconnect
4. **`websockets.py`** — resume message handling, hello extension
5. **`hijack.js`** — store/send token across reconnects
6. **CF `store.py`** — sqlite resume_tokens table
7. **CF `session_runtime.py`** — wire resume into webSocketMessage()
8. **Tests** — unit tests for store, WS route resume flow, hijack reclaim, edge cases, Playwright

## Verification

1. Unit tests: `InMemoryResumeStore` create/get/revoke/expire
2. WS route tests: connect → get token → disconnect → reconnect with token → verify role restored, hello has `resumed: true`
3. Hijack reclaim test: acquire hijack → disconnect → reconnect with token → verify hijack reclaimed
4. Expired token test: wait > TTL → reconnect → verify fresh flow
5. Wrong worker_id test: token from worker A used on worker B → fresh flow
6. Two-tab test: tab A resumes with token → tab B tries same token → fresh flow
7. Playwright: connect, kill WS in devtools, verify auto-reconnect restores state
8. CF E2E: same flow against live worker
