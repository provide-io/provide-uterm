# Connection Profiles — Design Spec

**Goal:** Add saved connection profiles to provide-terminal so it functions as a daily-driver browser SSH client, competitive with sshwifty, without adding credential storage complexity.

**Architecture:** A `FileProfileStore` backed by a single JSON file, wired into `ServerConfig` following the `RecordingConfig` pattern. Five new API endpoints under `/api/profiles`. Dashboard gains a Profiles section; the connect form gains pre-fill-from-profile and save-as-profile.

**Credential policy:** Profiles store host/port/username/tags/defaults — never passwords. "Connect from Profile" pre-fills the form; the user enters their password at connect time. This avoids encryption complexity and is correct for team use.

---

## Data Model

New file: `packages/provide-terminal/src/provide/terminal/server/profiles.py`

```python
class ConnectionProfile(ServerBaseModel):
    profile_id: str          # UUID
    owner: str               # principal.subject_id
    name: str                # display name
    connector_type: Literal["ssh", "telnet", "websocket", "ushell", "shell"]
    host: str | None = None
    port: int | None = None
    username: str | None = None
    tags: list[str] = []
    input_mode: Literal["open", "exclusive"] = "open"
    recording_enabled: bool = False
    visibility: Literal["private", "shared"] = "private"
    created_at: float
    updated_at: float
```

`FileProfileStore` stores all profiles in `.uterm-profiles/profiles.json`. Writes are atomic (temp file + `os.replace`). An `asyncio.Lock` serializes concurrent writes. On first read, a missing file returns an empty list — no error.

Config extension in `models.py`:

```python
class ProfileStoreConfig(ServerBaseModel):
    directory: Path = Path(".uterm-profiles")

class ServerConfig(ServerBaseModel):
    ...
    profiles: ProfileStoreConfig = Field(default_factory=ProfileStoreConfig)
```

The store is instantiated in `app.py` alongside the `SessionRegistry` and attached to `app.state`.

---

## API Endpoints

New router: `packages/provide-terminal/src/provide/terminal/server/routes/profiles.py`, mounted at `/api/profiles`.

| Method | Path | Auth | Behaviour |
|---|---|---|---|
| `GET` | `/api/profiles` | authenticated | Returns own profiles + shared profiles visible to principal |
| `POST` | `/api/profiles` | `can_create_session` | Creates profile; validates required fields per connector type |
| `PUT` | `/api/profiles/{profile_id}` | owner or admin | Partial update — only keys present in payload are changed |
| `DELETE` | `/api/profiles/{profile_id}` | owner or admin | Returns `{"ok": true}` |
| `POST` | `/api/profiles/{profile_id}/connect` | owner, shared+viewer, or admin | Merges profile into quick-connect payload; accepts optional `password` in body; returns `{"session_id": ..., "url": ...}` |

The `/connect` endpoint merges profile fields into a `quick_connect`-style payload and calls the registry exactly as `POST /api/connect` does. The connecting principal becomes `owner` of the new ephemeral session — not the profile owner. No new session creation logic.

Authorization helpers added to `authorization.py`:

```python
def can_read_profile(principal, profile) -> bool:
    return (
        profile.owner == principal.subject_id
        or profile.visibility == "shared"
        or is_admin(principal)
    )

def can_mutate_profile(principal, profile) -> bool:
    return profile.owner == principal.subject_id or is_admin(principal)
```

Error responses follow existing patterns: 404 for unknown profile ID, 403 for authorization failures, 422 for validation errors.

---

## Frontend

### New: `types.ts`
Add `ConnectionProfile` interface mirroring the Python model.

### New functions in `api.ts`
- `fetchProfiles(): Promise<ConnectionProfile[]>`
- `createProfile(payload): Promise<ConnectionProfile>`
- `deleteProfile(id): Promise<void>`
- `connectFromProfile(id, password?): Promise<{session_id: string, url: string}>`

### Modified: `dashboard-view.ts`
Add a **Profiles** section above the session groups. `loadDashboardState()` is extended to call `fetchProfiles()` in parallel with `fetchSessions()` via `Promise.all`.

Profile card content:
- Name (bold)
- Connector type + host (e.g. `ssh · example.com:22`)
- Tags
- Two buttons: **Connect** → navigates to `/app/connect?profile=<id>` | **Delete** → `DELETE /api/profiles/<id>` then re-renders

### Modified: `connect-view.ts`
Two additions:

1. **Pre-fill from profile**: on page load, if `?profile=<id>` is present in the query string, `fetchProfile(id)` is called and its fields populate the form (connector type, host, port, username, name, tags, input mode). Password field remains empty — user types it.

2. **Save as profile checkbox**: below the form. When checked and connect succeeds, a `POST /api/profiles` fires with all form fields except password, saving the profile for future use.

No new routes or nav entries. The connect form URL (`/app/connect`) already exists; pre-fill via `?profile=<id>` is clean and bookmarkable.

---

## Error Handling

- **Profile not found** → 404 (same `_sid_not_found` helper pattern as sessions)
- **Shared profile → connect** → connecting principal becomes session owner, not profile owner
- **Disk write failure** in `FileProfileStore` → bubble as 500; atomic write ensures no partial-write corruption
- **Missing profiles.json on startup** → `FileProfileStore.list_profiles()` returns `[]`, file created on first write

---

## Testing

### `tests/server/test_profile_store.py`
- CRUD roundtrip (create → get → update → delete)
- `list_profiles` filters correctly by owner and visibility
- Concurrent writes via `asyncio.gather` leave file consistent
- Missing file on first read returns empty list
- Atomic write: temp file is gone after successful write

### `tests/server/test_api_profiles.py`
Via `httpx.AsyncClient` against a test app:
- List: own profiles returned, shared visible to others, private not visible to others
- Create: valid payload succeeds, missing required fields → 422
- Update: owner can update, non-owner → 403, admin can update any
- Delete: owner can delete, non-owner → 403, unknown ID → 404
- `/connect`: creates an ephemeral session owned by the *connecting* principal (not profile owner); password passed in body is forwarded to connector config and not stored

### Coverage
`can_read_profile` and `can_mutate_profile` require explicit test cases for: own profile, shared + non-owner, private + non-owner (→ 403/404), admin override.

---

## File Map

| File | Action |
|---|---|
| `packages/provide-terminal/src/provide/terminal/server/profiles.py` | Create — `ConnectionProfile` model + `FileProfileStore` |
| `packages/provide-terminal/src/provide/terminal/server/routes/profiles.py` | Create — 5 API endpoints |
| `packages/provide-terminal/src/provide/terminal/server/models.py` | Modify — add `ProfileStoreConfig` to `ServerConfig` |
| `packages/provide-terminal/src/provide/terminal/server/authorization.py` | Modify — add `can_read_profile`, `can_mutate_profile` |
| `packages/provide-terminal/src/provide/terminal/server/app.py` | Modify — instantiate `FileProfileStore`, mount profiles router |
| `packages/provide-terminal-frontend/src/app/types.ts` | Modify — add `ConnectionProfile` interface |
| `packages/provide-terminal-frontend/src/app/api.ts` | Modify — add 4 profile API functions |
| `packages/provide-terminal-frontend/src/app/views/dashboard-view.ts` | Modify — add Profiles section, parallel fetch |
| `packages/provide-terminal-frontend/src/app/views/connect-view.ts` | Modify — pre-fill from `?profile=<id>`, save-as-profile checkbox |
| `tests/server/test_profile_store.py` | Create |
| `tests/server/test_api_profiles.py` | Create |
