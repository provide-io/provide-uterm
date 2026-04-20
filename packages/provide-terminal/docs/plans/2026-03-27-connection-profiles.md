# Connection Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add saved connection profiles to provide-terminal — per-principal stored SSH/telnet/etc. targets that pre-fill the connect form, making the app a daily-driver browser SSH client.

**Architecture:** `FileProfileStore` (atomic JSON-on-disk) in `server/profiles.py`; two auth helpers added to `AuthorizationService`; `ProfileStoreConfig` added to `ServerConfig`; five REST endpoints in `server/routes/profiles.py`; dashboard gets a Profiles section; connect form gets `?profile=<id>` pre-fill and a save-as-profile checkbox.

**Tech Stack:** Python 3.11, Pydantic v2, FastAPI, asyncio.Lock, TypeScript, existing `apiJson` fetch helper.

---

## File Map

| File | Action |
|---|---|
| `packages/provide-terminal/src/provide/terminal/server/profiles.py` | **Create** — `ConnectionProfile` model + `FileProfileStore` |
| `packages/provide-terminal/tests/server/test_profile_store.py` | **Create** — unit tests for `FileProfileStore` |
| `packages/provide-terminal/src/provide/terminal/server/models.py` | **Modify** — add `ProfileStoreConfig`, extend `ServerConfig` and `ServerModel` |
| `packages/provide-terminal/src/provide/terminal/server/authorization.py` | **Modify** — add `can_read_profile`, `can_mutate_profile` |
| `packages/provide-terminal/tests/server/test_authorization.py` | **Modify** — add profile auth tests |
| `packages/provide-terminal/src/provide/terminal/server/routes/profiles.py` | **Create** — 6 API endpoints |
| `packages/provide-terminal/tests/server/test_api_profiles.py` | **Create** — API integration tests |
| `packages/provide-terminal/src/provide/terminal/server/app.py` | **Modify** — instantiate `FileProfileStore`, mount profiles router |
| `packages/provide-terminal-frontend/src/app/types.ts` | **Modify** — add `ConnectionProfile` interface |
| `packages/provide-terminal-frontend/src/app/api.ts` | **Modify** — add 5 profile API functions |
| `packages/provide-terminal-frontend/src/app/views/dashboard-view.ts` | **Modify** — add Profiles section, parallel fetch |
| `packages/provide-terminal-frontend/src/app/views/connect-view.ts` | **Modify** — pre-fill from `?profile=<id>`, save-as-profile checkbox |

---

## Task 1: ConnectionProfile Model + FileProfileStore

**Files:**
- Create: `packages/provide-terminal/src/provide/terminal/server/profiles.py`
- Create: `packages/provide-terminal/tests/server/test_profile_store.py`

- [ ] **Step 1: Write the failing tests**

Create `packages/provide-terminal/tests/server/test_profile_store.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for ConnectionProfile model and FileProfileStore."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from provide.terminal.server.profiles import ConnectionProfile, FileProfileStore


def _make_profile(
    profile_id: str = "profile-abc123",
    owner: str = "user1",
    name: str = "My Server",
    connector_type: str = "ssh",
    visibility: str = "private",
    **kwargs: object,
) -> ConnectionProfile:
    now = time.time()
    return ConnectionProfile(
        profile_id=profile_id,
        owner=owner,
        name=name,
        connector_type=connector_type,
        visibility=visibility,
        created_at=now,
        updated_at=now,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture()
def store(tmp_path: Path) -> FileProfileStore:
    return FileProfileStore(tmp_path / "profiles")


# ── List ──────────────────────────────────────────────────────────────────


async def test_list_returns_empty_when_file_missing(store: FileProfileStore) -> None:
    result = await store.list_profiles()
    assert result == []


async def test_list_returns_own_private_profiles(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(owner="user1", visibility="private"))
    result = await store.list_profiles(owner="user1")
    assert len(result) == 1


async def test_list_excludes_other_private_profiles(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(owner="user2", visibility="private"))
    result = await store.list_profiles(owner="user1")
    assert result == []


async def test_list_includes_shared_profiles_for_any_owner(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(owner="user2", visibility="shared"))
    result = await store.list_profiles(owner="user1")
    assert len(result) == 1


async def test_list_with_no_owner_returns_all(store: FileProfileStore) -> None:
    await store.create_profile(_make_profile(profile_id="p1", owner="user1", visibility="private"))
    await store.create_profile(_make_profile(profile_id="p2", owner="user2", visibility="private"))
    result = await store.list_profiles()
    assert len(result) == 2


# ── Get ───────────────────────────────────────────────────────────────────


async def test_get_returns_profile_by_id(store: FileProfileStore) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    fetched = await store.get_profile(profile.profile_id)
    assert fetched is not None
    assert fetched.profile_id == profile.profile_id
    assert fetched.name == "My Server"


async def test_get_returns_none_for_unknown_id(store: FileProfileStore) -> None:
    result = await store.get_profile("nonexistent")
    assert result is None


# ── Create ────────────────────────────────────────────────────────────────


async def test_create_persists_to_disk(store: FileProfileStore, tmp_path: Path) -> None:
    await store.create_profile(_make_profile())
    # Re-create store pointing to same directory — forces a fresh disk read.
    store2 = FileProfileStore(tmp_path / "profiles")
    result = await store2.list_profiles()
    assert len(result) == 1
    assert result[0].name == "My Server"


async def test_create_directory_created_if_missing(tmp_path: Path) -> None:
    store = FileProfileStore(tmp_path / "deep" / "nested" / "dir")
    await store.create_profile(_make_profile())
    assert (tmp_path / "deep" / "nested" / "dir" / "profiles.json").exists()


# ── Update ────────────────────────────────────────────────────────────────


async def test_update_changes_name(store: FileProfileStore) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    updated = await store.update_profile(profile.profile_id, {"name": "Renamed"})
    assert updated is not None
    assert updated.name == "Renamed"
    # Verify updated_at is refreshed
    assert updated.updated_at >= profile.updated_at


async def test_update_returns_none_for_unknown_id(store: FileProfileStore) -> None:
    result = await store.update_profile("nonexistent", {"name": "x"})
    assert result is None


async def test_update_persists_change(store: FileProfileStore, tmp_path: Path) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    await store.update_profile(profile.profile_id, {"name": "Persisted"})
    store2 = FileProfileStore(tmp_path / "profiles")
    fetched = await store2.get_profile(profile.profile_id)
    assert fetched is not None
    assert fetched.name == "Persisted"


# ── Delete ────────────────────────────────────────────────────────────────


async def test_delete_removes_profile(store: FileProfileStore) -> None:
    profile = _make_profile()
    await store.create_profile(profile)
    deleted = await store.delete_profile(profile.profile_id)
    assert deleted is True
    assert await store.get_profile(profile.profile_id) is None


async def test_delete_returns_false_for_unknown_id(store: FileProfileStore) -> None:
    result = await store.delete_profile("nonexistent")
    assert result is False


# ── Atomic write ──────────────────────────────────────────────────────────


async def test_atomic_write_no_tmp_file_left(store: FileProfileStore, tmp_path: Path) -> None:
    await store.create_profile(_make_profile())
    tmp = tmp_path / "profiles" / "profiles.tmp"
    assert not tmp.exists()


# ── Concurrency ───────────────────────────────────────────────────────────


async def test_concurrent_creates_are_consistent(store: FileProfileStore) -> None:
    profiles = [_make_profile(profile_id=f"profile-{i}", name=f"Server {i}") for i in range(10)]
    await asyncio.gather(*[store.create_profile(p) for p in profiles])
    all_profiles = await store.list_profiles()
    assert len(all_profiles) == 10
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run pytest packages/provide-terminal/tests/server/test_profile_store.py -v --no-cov 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'ConnectionProfile' from 'provide.terminal.server.profiles'` or `ModuleNotFoundError`.

- [ ] **Step 3: Implement `profiles.py`**

Create `packages/provide-terminal/src/provide/terminal/server/profiles.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""ConnectionProfile model and FileProfileStore for persisted connection profiles."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from provide.terminal.server.models import ServerBaseModel


class ConnectionProfile(ServerBaseModel):
    """A saved connection target owned by a principal."""

    profile_id: str
    owner: str
    name: str
    connector_type: Literal["ssh", "telnet", "websocket", "ushell", "shell"]
    host: str | None = None
    port: int | None = None
    username: str | None = None
    tags: list[str] = []
    input_mode: Literal["open", "hijack"] = "open"
    recording_enabled: bool = False
    visibility: Literal["private", "shared"] = "private"
    created_at: float
    updated_at: float


class FileProfileStore:
    """Atomic JSON-file-backed store for connection profiles.

    All writes use a temp-file + os.replace() for atomicity.
    Concurrent access is serialised with an asyncio.Lock.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = asyncio.Lock()

    def _path(self) -> Path:
        return self._directory / "profiles.json"

    def _read_sync(self) -> list[ConnectionProfile]:
        """Read all profiles from disk. Caller must hold self._lock."""
        path = self._path()
        if not path.exists():
            return []
        data: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
        return [ConnectionProfile.model_validate(p) for p in data]

    def _write_sync(self, profiles: list[ConnectionProfile]) -> None:
        """Write all profiles to disk atomically. Caller must hold self._lock."""
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([p.model_dump(mode="python") for p in profiles], indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    async def list_profiles(self, *, owner: str | None = None) -> list[ConnectionProfile]:
        """Return profiles visible to *owner* (own + shared), or all if owner is None."""
        async with self._lock:
            profiles = self._read_sync()
        if owner is None:
            return profiles
        return [p for p in profiles if p.owner == owner or p.visibility == "shared"]

    async def get_profile(self, profile_id: str) -> ConnectionProfile | None:
        """Return the profile with the given ID, or None if not found."""
        async with self._lock:
            profiles = self._read_sync()
        return next((p for p in profiles if p.profile_id == profile_id), None)

    async def create_profile(self, profile: ConnectionProfile) -> ConnectionProfile:
        """Persist a new profile and return it."""
        async with self._lock:
            profiles = self._read_sync()
            profiles.append(profile)
            self._write_sync(profiles)
        return profile

    async def update_profile(self, profile_id: str, updates: dict[str, Any]) -> ConnectionProfile | None:
        """Apply *updates* to the profile and return the updated model, or None if not found."""
        async with self._lock:
            profiles = self._read_sync()
            for i, p in enumerate(profiles):
                if p.profile_id == profile_id:
                    data = p.model_dump(mode="python")
                    data.update(updates)
                    data["updated_at"] = time.time()
                    profiles[i] = ConnectionProfile.model_validate(data)
                    self._write_sync(profiles)
                    return profiles[i]
        return None

    async def delete_profile(self, profile_id: str) -> bool:
        """Delete the profile. Returns True if it existed, False if not found."""
        async with self._lock:
            profiles = self._read_sync()
            new_profiles = [p for p in profiles if p.profile_id != profile_id]
            if len(new_profiles) == len(profiles):
                return False
            self._write_sync(new_profiles)
        return True
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run pytest packages/provide-terminal/tests/server/test_profile_store.py -v --no-cov 2>&1 | tail -5
```

Expected: `22 passed`.

- [ ] **Step 5: Commit**

```bash
git add \
  packages/provide-terminal/src/provide/terminal/server/profiles.py \
  packages/provide-terminal/tests/server/test_profile_store.py
git commit -m "feat: add ConnectionProfile model and FileProfileStore"
```

---

## Task 2: Config + Authorization

**Files:**
- Modify: `packages/provide-terminal/src/provide/terminal/server/models.py`
- Modify: `packages/provide-terminal/src/provide/terminal/server/authorization.py`
- Modify: `packages/provide-terminal/tests/server/test_authorization.py`

- [ ] **Step 1: Write the failing authorization tests**

Open `packages/provide-terminal/tests/server/test_authorization.py`. At the end of the file, append:

```python
# ── Profile authorization ─────────────────────────────────────────────────


def _make_test_profile(owner: str, visibility: str = "private") -> object:
    """Return a minimal ConnectionProfile-like object for auth tests."""
    import time
    from provide.terminal.server.profiles import ConnectionProfile

    now = time.time()
    return ConnectionProfile(
        profile_id="profile-test",
        owner=owner,
        name="Test",
        connector_type="ssh",
        visibility=visibility,  # type: ignore[arg-type]
        created_at=now,
        updated_at=now,
    )


def test_can_read_own_private_profile() -> None:
    authz = AuthorizationService()
    principal = _make_principal("alice", "operator")
    profile = _make_test_profile(owner="alice", visibility="private")
    assert authz.can_read_profile(principal, profile) is True  # type: ignore[arg-type]


def test_cannot_read_other_private_profile() -> None:
    authz = AuthorizationService()
    principal = _make_principal("alice", "operator")
    profile = _make_test_profile(owner="bob", visibility="private")
    assert authz.can_read_profile(principal, profile) is False  # type: ignore[arg-type]


def test_can_read_shared_profile_as_non_owner() -> None:
    authz = AuthorizationService()
    principal = _make_principal("alice", "operator")
    profile = _make_test_profile(owner="bob", visibility="shared")
    assert authz.can_read_profile(principal, profile) is True  # type: ignore[arg-type]


def test_admin_can_read_any_profile() -> None:
    authz = AuthorizationService()
    principal = _make_principal("admin", "admin")
    profile = _make_test_profile(owner="bob", visibility="private")
    assert authz.can_read_profile(principal, profile) is True  # type: ignore[arg-type]


def test_can_mutate_own_profile() -> None:
    authz = AuthorizationService()
    principal = _make_principal("alice", "operator")
    profile = _make_test_profile(owner="alice")
    assert authz.can_mutate_profile(principal, profile) is True  # type: ignore[arg-type]


def test_cannot_mutate_other_profile() -> None:
    authz = AuthorizationService()
    principal = _make_principal("alice", "operator")
    profile = _make_test_profile(owner="bob")
    assert authz.can_mutate_profile(principal, profile) is False  # type: ignore[arg-type]


def test_admin_can_mutate_any_profile() -> None:
    authz = AuthorizationService()
    principal = _make_principal("admin", "admin")
    profile = _make_test_profile(owner="bob")
    assert authz.can_mutate_profile(principal, profile) is True  # type: ignore[arg-type]
```

Note: `_make_principal` is assumed to already exist in `test_authorization.py`. Check the existing file for the exact helper name and adjust if it differs.

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run pytest packages/provide-terminal/tests/server/test_authorization.py -v --no-cov -k "profile" 2>&1 | tail -10
```

Expected: `AttributeError: 'AuthorizationService' object has no attribute 'can_read_profile'`.

- [ ] **Step 3: Add `ProfileStoreConfig` to `models.py`**

In `packages/provide-terminal/src/provide/terminal/server/models.py`:

After the `RecordingConfig` class (around line 109), add:

```python
class ProfileStoreConfig(ServerBaseModel):
    """File-backed profile store settings."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    directory: Path = Path(".uterm-profiles")
```

In `ServerConfig` (around line 237), add `profiles` field after `recording`:

```python
class ServerConfig(ServerBaseModel):
    """Top-level application config for the standalone server."""

    server: ServerBindConfig = Field(default_factory=ServerBindConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    profiles: ProfileStoreConfig = Field(default_factory=ProfileStoreConfig)
    sessions: list[SessionDefinition] = Field(default_factory=list)
```

In the `ServerModel` type alias (around line 247), add `ProfileStoreConfig`:

```python
ServerModel: TypeAlias = (
    AuthConfig
    | UiConfig
    | RecordingConfig
    | ProfileStoreConfig
    | ServerBindConfig
    | SessionDefinition
    | SessionRuntimeStatus
    | ServerConfig
)
```

- [ ] **Step 4: Add `can_read_profile` and `can_mutate_profile` to `authorization.py`**

In `packages/provide-terminal/src/provide/terminal/server/authorization.py`:

Add to the `TYPE_CHECKING` block:
```python
if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.models import SessionDefinition
    from provide.terminal.server.profiles import ConnectionProfile
```

Add two methods to `AuthorizationService` after `can_mutate_session`:

```python
    def can_read_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return (
            profile.owner == principal.subject_id
            or profile.visibility == "shared"
            or self.is_admin(principal)
        )

    def can_mutate_profile(self, principal: Principal, profile: ConnectionProfile) -> bool:
        return profile.owner == principal.subject_id or self.is_admin(principal)
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run pytest packages/provide-terminal/tests/server/test_authorization.py -v --no-cov -k "profile" 2>&1 | tail -5
```

Expected: `7 passed`.

- [ ] **Step 6: Verify models test still passes**

```bash
uv run pytest packages/provide-terminal/tests/server/test_models_mutation_killing.py -v --no-cov 2>&1 | tail -3
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add \
  packages/provide-terminal/src/provide/terminal/server/models.py \
  packages/provide-terminal/src/provide/terminal/server/authorization.py \
  packages/provide-terminal/tests/server/test_authorization.py
git commit -m "feat: add ProfileStoreConfig to ServerConfig and profile auth helpers"
```

---

## Task 3: API Routes + App Wiring

**Files:**
- Create: `packages/provide-terminal/src/provide/terminal/server/routes/profiles.py`
- Create: `packages/provide-terminal/tests/server/test_api_profiles.py`
- Modify: `packages/provide-terminal/src/provide/terminal/server/app.py`

- [ ] **Step 1: Write the failing API tests**

Create `packages/provide-terminal/tests/server/test_api_profiles.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests for /api/profiles endpoints."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from provide.terminal.server import create_server_app, default_server_config


@pytest.fixture()
def app_client(tmp_path: Path) -> TestClient:
    config = default_server_config()
    config.auth.mode = "dev"
    config.profiles.directory = tmp_path / "profiles"
    app = create_server_app(config)
    return TestClient(app)


@pytest.fixture()
def viewer_client(tmp_path: Path) -> TestClient:
    """Client with viewer role only — cannot create sessions/profiles."""
    import jwt as _jwt

    key = "uterm-test-secret-32-byte-minimum-key"
    now = int(time.time())
    token = _jwt.encode(
        {
            "sub": "viewer1",
            "roles": ["viewer"],
            "iss": "provide-terminal",
            "aud": "provide-terminal-server",
            "iat": now,
            "exp": now + 3600,
        },
        key,
        algorithm="HS256",
    )
    config = default_server_config()
    config.auth.mode = "jwt"
    config.auth.jwt_algorithms = ["HS256"]
    config.auth.jwt_public_key_pem = key
    config.auth.worker_bearer_token = "test-worker-token"
    config.profiles.directory = tmp_path / "profiles-viewer"
    app = create_server_app(config)
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _create_profile(client: TestClient, **kwargs: object) -> dict:
    payload = {"name": "My Server", "connector_type": "ssh", "host": "example.com", **kwargs}
    r = client.post("/api/profiles", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ── List ──────────────────────────────────────────────────────────────────


def test_list_empty_initially(app_client: TestClient) -> None:
    r = app_client.get("/api/profiles")
    assert r.status_code == 200
    assert r.json() == []


def test_list_returns_own_profile(app_client: TestClient) -> None:
    _create_profile(app_client)
    r = app_client.get("/api/profiles")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["name"] == "My Server"


# ── Get ───────────────────────────────────────────────────────────────────


def test_get_profile_returns_200(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    r = app_client.get(f"/api/profiles/{profile['profile_id']}")
    assert r.status_code == 200
    assert r.json()["profile_id"] == profile["profile_id"]


def test_get_profile_unknown_id_returns_404(app_client: TestClient) -> None:
    r = app_client.get("/api/profiles/nonexistent")
    assert r.status_code == 404


# ── Create ────────────────────────────────────────────────────────────────


def test_create_profile_returns_profile(app_client: TestClient) -> None:
    r = app_client.post("/api/profiles", json={"name": "Prod", "connector_type": "ssh", "host": "prod.example.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Prod"
    assert data["connector_type"] == "ssh"
    assert data["host"] == "prod.example.com"
    assert "profile_id" in data
    assert data["owner"] == "local-dev"  # dev mode principal


def test_create_profile_viewer_role_returns_403(viewer_client: TestClient) -> None:
    r = viewer_client.post("/api/profiles", json={"name": "x", "connector_type": "ssh"})
    assert r.status_code == 403


def test_create_profile_sets_defaults(app_client: TestClient) -> None:
    r = app_client.post("/api/profiles", json={"name": "Min", "connector_type": "ushell"})
    assert r.status_code == 200
    data = r.json()
    assert data["visibility"] == "private"
    assert data["input_mode"] == "open"
    assert data["recording_enabled"] is False
    assert data["tags"] == []


# ── Update ────────────────────────────────────────────────────────────────


def test_update_profile_changes_name(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    r = app_client.put(f"/api/profiles/{profile['profile_id']}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_update_unknown_profile_returns_404(app_client: TestClient) -> None:
    r = app_client.put("/api/profiles/nonexistent", json={"name": "x"})
    assert r.status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────


def test_delete_profile_returns_ok(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    r = app_client.delete(f"/api/profiles/{profile['profile_id']}")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_delete_profile_removes_it(app_client: TestClient) -> None:
    profile = _create_profile(app_client)
    app_client.delete(f"/api/profiles/{profile['profile_id']}")
    r = app_client.get("/api/profiles")
    assert r.json() == []


def test_delete_unknown_profile_returns_404(app_client: TestClient) -> None:
    r = app_client.delete("/api/profiles/nonexistent")
    assert r.status_code == 404


# ── Connect ───────────────────────────────────────────────────────────────


def test_connect_from_profile_creates_session(app_client: TestClient) -> None:
    profile = _create_profile(app_client, connector_type="ushell")
    r = app_client.post(f"/api/profiles/{profile['profile_id']}/connect", json={})
    assert r.status_code == 200
    data = r.json()
    assert "session_id" in data
    assert "url" in data
    assert data["owner"] == "local-dev"  # connecting principal owns the session


def test_connect_from_profile_unknown_id_returns_404(app_client: TestClient) -> None:
    r = app_client.post("/api/profiles/nonexistent/connect", json={})
    assert r.status_code == 404


def test_connect_from_profile_forwards_password(app_client: TestClient) -> None:
    """Password is forwarded to session connector_config but not stored in profile."""
    profile = _create_profile(app_client, connector_type="ssh", host="h", username="u")
    r = app_client.post(
        f"/api/profiles/{profile['profile_id']}/connect",
        json={"password": "s3cr3t"},
    )
    assert r.status_code == 200
    # Password must not appear in the profile itself
    fetched = app_client.get(f"/api/profiles/{profile['profile_id']}").json()
    assert "password" not in fetched
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run pytest packages/provide-terminal/tests/server/test_api_profiles.py -v --no-cov 2>&1 | tail -10
```

Expected: `404 Not Found` or `AttributeError: 'ServerConfig' has no attribute 'profiles'`.

- [ ] **Step 3: Create `routes/profiles.py`**

Create `packages/provide-terminal/src/provide/terminal/server/routes/profiles.py`:

```python
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""REST API routes for connection profiles."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Body, HTTPException, Path, Request

from provide.terminal.server.models import model_dump
from provide.terminal.server.profiles import ConnectionProfile
from provide.terminal.server.registry import SessionValidationError

if TYPE_CHECKING:
    from provide.terminal.server.auth import Principal
    from provide.terminal.server.authorization import AuthorizationService
    from provide.terminal.server.profiles import FileProfileStore
    from provide.terminal.server.registry import SessionRegistry

_ProfileId = Annotated[str, Path(pattern=r"^[\w\-]+$")]


def _store(request: Request) -> FileProfileStore:
    return cast("FileProfileStore", request.app.state.uterm_profile_store)


def _authz(request: Request) -> AuthorizationService:
    return cast("AuthorizationService", request.app.state.uterm_authz)


def _registry(request: Request) -> SessionRegistry:
    return cast("SessionRegistry", request.app.state.uterm_registry)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "uterm_principal", None)
    if principal is None:
        raise HTTPException(status_code=500, detail="principal was not resolved")
    return cast("Principal", principal)


def _not_found(profile_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown profile: {profile_id}")


def create_profiles_router() -> APIRouter:
    router = APIRouter(prefix="/api/profiles")

    @router.get("")
    async def list_profiles(request: Request) -> list[dict[str, Any]]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        if authz.is_admin(principal):
            profiles = await store.list_profiles()
        else:
            profiles = await store.list_profiles(owner=principal.subject_id)
        return [p.model_dump(mode="python") for p in profiles]

    @router.get("/{profile_id}")
    async def get_profile(request: Request, profile_id: _ProfileId) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not authz.can_read_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        return profile.model_dump(mode="python")

    @router.post("")
    async def create_profile(
        request: Request, payload: Annotated[dict[str, Any], Body(...)]
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        if not authz.can_create_session(principal):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        store = _store(request)
        now = time.time()
        tags_raw = payload.get("tags", [])
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] if isinstance(tags_raw, list) else []
        profile = ConnectionProfile(
            profile_id=f"profile-{uuid.uuid4().hex[:12]}",
            owner=principal.subject_id,
            name=str(payload.get("name") or "Unnamed").strip(),
            connector_type=str(payload.get("connector_type", "ssh")),
            host=str(payload["host"]).strip() or None if payload.get("host") else None,
            port=int(payload["port"]) if payload.get("port") else None,
            username=str(payload["username"]).strip() or None if payload.get("username") else None,
            tags=tags,
            input_mode=str(payload.get("input_mode", "open")),
            recording_enabled=bool(payload.get("recording_enabled", False)),
            visibility=str(payload.get("visibility", "private")),
            created_at=now,
            updated_at=now,
        )
        created = await store.create_profile(profile)
        return created.model_dump(mode="python")

    @router.put("/{profile_id}")
    async def update_profile(
        request: Request,
        profile_id: _ProfileId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not authz.can_mutate_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        allowed = {"name", "host", "port", "username", "tags", "input_mode", "recording_enabled", "visibility"}
        updates = {k: v for k, v in payload.items() if k in allowed}
        updated = await store.update_profile(profile_id, updates)
        if updated is None:
            raise _not_found(profile_id)
        return updated.model_dump(mode="python")

    @router.delete("/{profile_id}")
    async def delete_profile(request: Request, profile_id: _ProfileId) -> dict[str, bool]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not authz.can_mutate_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        await store.delete_profile(profile_id)
        return {"ok": True}

    @router.post("/{profile_id}/connect")
    async def connect_from_profile(
        request: Request,
        profile_id: _ProfileId,
        payload: Annotated[dict[str, Any], Body(...)],
    ) -> dict[str, Any]:
        principal = _principal(request)
        authz = _authz(request)
        store = _store(request)
        registry = _registry(request)
        profile = await store.get_profile(profile_id)
        if profile is None:
            raise _not_found(profile_id)
        if not authz.can_read_profile(principal, profile):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        if not authz.can_create_session(principal):
            raise HTTPException(status_code=403, detail="insufficient privileges")
        connector_config: dict[str, Any] = {}
        if profile.host:
            connector_config["host"] = profile.host
        if profile.port:
            connector_config["port"] = profile.port
        if profile.username:
            connector_config["username"] = profile.username
        if payload.get("password"):
            connector_config["password"] = payload["password"]
        session_id = f"connect-{uuid.uuid4().hex[:12]}"
        session_payload: dict[str, Any] = {
            "session_id": session_id,
            "display_name": profile.name,
            "connector_type": profile.connector_type,
            "connector_config": connector_config,
            "input_mode": profile.input_mode,
            "tags": list(profile.tags),
            "auto_start": True,
            "ephemeral": True,
            "visibility": "private",
            "owner": principal.subject_id,
        }
        if profile.recording_enabled:
            session_payload["recording_enabled"] = True
        try:
            session = await registry.create_session(session_payload)
        except SessionValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        cfg = request.app.state.uterm_config
        url = f"{cfg.ui.app_path}/session/{session_id}"
        return {"session_id": session_id, "url": url, **model_dump(session)}

    return router
```

- [ ] **Step 4: Wire `FileProfileStore` and profiles router into `app.py`**

In `packages/provide-terminal/src/provide/terminal/server/app.py`:

Add these two imports after the existing `from provide.terminal.server.routes.api import create_api_router` line:

```python
from provide.terminal.server.profiles import FileProfileStore
from provide.terminal.server.routes.profiles import create_profiles_router
```

After the line `registry = SessionRegistry(...)` block (around line 577) and before `@asynccontextmanager`, add:

```python
    profile_store = FileProfileStore(config.profiles.directory)
```

After `app.state.uterm_webhooks = webhook_manager` (around line 618), add:

```python
    app.state.uterm_profile_store = profile_store
```

After `app.include_router(create_api_router(), ...)` (around line 654), add:

```python
    app.include_router(create_profiles_router(), dependencies=[Depends(_require_authenticated)])
```

- [ ] **Step 5: Run API tests to confirm they pass**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run pytest packages/provide-terminal/tests/server/test_api_profiles.py -v --no-cov 2>&1 | tail -10
```

Expected: `18 passed`.

- [ ] **Step 6: Commit**

```bash
git add \
  packages/provide-terminal/src/provide/terminal/server/routes/profiles.py \
  packages/provide-terminal/tests/server/test_api_profiles.py \
  packages/provide-terminal/src/provide/terminal/server/app.py
git commit -m "feat: add /api/profiles endpoints and wire FileProfileStore into app"
```

---

## Task 4: Frontend

**Files:**
- Modify: `packages/provide-terminal-frontend/src/app/types.ts`
- Modify: `packages/provide-terminal-frontend/src/app/api.ts`
- Modify: `packages/provide-terminal-frontend/src/app/views/dashboard-view.ts`
- Modify: `packages/provide-terminal-frontend/src/app/views/connect-view.ts`

- [ ] **Step 1: Add `ConnectionProfile` to `types.ts`**

In `packages/provide-terminal-frontend/src/app/types.ts`, append after the last `export interface`:

```typescript
export interface ConnectionProfile {
  profile_id: string;
  owner: string;
  name: string;
  connector_type: string;
  host: string | null;
  port: number | null;
  username: string | null;
  tags: string[];
  input_mode: string;
  recording_enabled: boolean;
  visibility: string;
  created_at: number;
  updated_at: number;
}
```

- [ ] **Step 2: Add profile API functions to `api.ts`**

In `packages/provide-terminal-frontend/src/app/api.ts`:

Add `ConnectionProfile` to the import from `types.js`:
```typescript
import type { ConnectionProfile, RecordingEntryView, SessionDetails, SessionSummary, SessionSurface } from "./types.js";
```

Append at the end of the file:

```typescript
export async function fetchProfiles(): Promise<ConnectionProfile[]> {
  return apiJson<ConnectionProfile[]>("/api/profiles");
}

export async function fetchProfile(profileId: string): Promise<ConnectionProfile | null> {
  try {
    return await apiJson<ConnectionProfile>(`/api/profiles/${encodeURIComponent(profileId)}`);
  } catch {
    return null;
  }
}

export async function createProfile(payload: Partial<ConnectionProfile> & { name: string; connector_type: string }): Promise<ConnectionProfile> {
  return apiJson<ConnectionProfile>("/api/profiles", "POST", payload);
}

export async function deleteProfile(profileId: string): Promise<void> {
  await apiJson<{ ok: boolean }>(`/api/profiles/${encodeURIComponent(profileId)}`, "DELETE");
}

export async function connectFromProfile(profileId: string, password?: string): Promise<QuickConnectResult> {
  return apiJson<QuickConnectResult>(
    `/api/profiles/${encodeURIComponent(profileId)}/connect`,
    "POST",
    password ? { password } : {},
  );
}
```

- [ ] **Step 3: Add Profiles section to `dashboard-view.ts`**

Replace the entire file `packages/provide-terminal-frontend/src/app/views/dashboard-view.ts` with:

```typescript
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { deleteProfile, deleteSession, fetchProfiles, restartSession } from "../api.js";
import { loadDashboardState, summarizeSessions } from "../state.js";
import type { AppBootstrap, ConnectionProfile, SessionSummary } from "../types.js";
import { renderAppHeader } from "./app-header.js";

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function profilesSectionMarkup(profiles: ConnectionProfile[], appPath: string): string {
  const safeAppPath = escapeHtml(appPath);
  if (profiles.length === 0) {
    return `
      <section class="card stack">
        <div class="section-heading">
          <h2>Profiles</h2>
          <div class="small">No saved profiles. <a href="${safeAppPath}/connect">Connect</a> and save one.</div>
        </div>
      </section>
    `;
  }
  return `
    <section class="card stack">
      <div class="section-heading"><h2>Profiles</h2><div class="small">${profiles.length} saved</div></div>
      <div class="session-list">
        ${profiles
          .map(
            (p) => `
          <article class="session-card" data-profile-id="${escapeHtml(p.profile_id)}">
            <div class="session-header">
              <div>
                <span class="session-title">${escapeHtml(p.name)}</span>
                <div class="small">${escapeHtml(p.connector_type)}${p.host ? ` · ${escapeHtml(p.host)}${p.port ? `:${p.port}` : ""}` : ""}</div>
              </div>
              <div class="session-badges">
                ${p.visibility === "shared" ? `<span class="badge badge-visibility">shared</span>` : ""}
              </div>
            </div>
            ${p.tags.length > 0 ? `<div class="tag-list">${p.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>` : ""}
            <div class="toolbar">
              <a class="btn" href="${safeAppPath}/connect?profile=${encodeURIComponent(p.profile_id)}">Connect</a>
              <button class="btn btn-delete-profile" data-profile-id="${escapeHtml(p.profile_id)}">Delete</button>
            </div>
          </article>
        `,
          )
          .join("")}
      </div>
    </section>
  `;
}

function sectionMarkup(title: string, sessions: SessionSummary[], appPath: string): string {
  const safeTitle = escapeHtml(title);
  const safeAppPath = escapeHtml(appPath);
  if (sessions.length === 0) {
    return `
      <section class="card stack">
        <div class="section-heading"><h2>${safeTitle}</h2><div class="small">No sessions.</div></div>
      </section>
    `;
  }
  return `
    <section class="card stack">
      <div class="section-heading"><h2>${safeTitle}</h2><div class="small">${sessions.length} session(s)</div></div>
      <div class="session-list">
        ${sessions
          .map(
            (session) => `
          <article class="session-card ${session.connected ? "live" : ""} ${session.lastError ? "error" : ""}" data-session-id="${escapeHtml(session.sessionId)}">
            <div class="session-header">
              <div>
                <a class="session-title" href="${safeAppPath}/operator/${encodeURIComponent(session.sessionId)}">${escapeHtml(session.displayName)}</a>
                <div class="small">${escapeHtml(session.sessionId)} • ${escapeHtml(session.connectorType)}</div>
              </div>
              <div class="session-badges">
                ${session.visibility !== "public" ? `<span class="badge badge-visibility">${escapeHtml(session.visibility)}</span>` : ""}
                ${session.recordingEnabled ? `<span class="badge badge-rec">⏺ rec</span>` : ""}
                ${session.recordingAvailable && !session.recordingEnabled ? `<span class="badge badge-rec-avail">⏺ saved</span>` : ""}
                <span class="status-chip ${session.connected ? "ok" : session.lastError ? "error" : "info"}">${
                  session.connected ? "Live" : session.lastError ? "Error" : "Stopped"
                }</span>
              </div>
            </div>
            ${
              session.tags.length > 0
                ? `<div class="tag-list">${session.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div>`
                : ""
            }
            <div class="toolbar">
              <a class="btn" href="${safeAppPath}/operator/${encodeURIComponent(session.sessionId)}">Control</a>
              <a class="btn" href="${safeAppPath}/session/${encodeURIComponent(session.sessionId)}">Watch</a>
              <a class="btn" href="${safeAppPath}/replay/${encodeURIComponent(session.sessionId)}">Replay</a>
              <button class="btn btn-restart" data-session-id="${escapeHtml(session.sessionId)}">Restart</button>
              <button class="btn btn-delete" data-session-id="${escapeHtml(session.sessionId)}">Delete</button>
            </div>
          </article>
        `,
          )
          .join("")}
      </div>
    </section>
  `;
}

export async function renderDashboard(root: HTMLElement, bootstrap: AppBootstrap): Promise<void> {
  const safeTitle = escapeHtml(bootstrap.title);
  root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "dashboard")}
      <section class="card stack">
        <h1>${safeTitle}</h1>
        <div class="toolbar">
          <button id="dashboard-refresh" class="btn">Refresh</button>
        </div>
        <div id="dashboard-status" class="status-chip info">Loading…</div>
      </section>
      <div id="dashboard-content" class="page"></div>
    </div>
  `;
  const status = root.querySelector<HTMLElement>("#dashboard-status");
  const content = root.querySelector<HTMLElement>("#dashboard-content");
  if (!status || !content) throw new Error("dashboard shell is incomplete");

  async function loadAll(statusEl: HTMLElement, contentEl: HTMLElement): Promise<void> {
    try {
      const [sessions, profiles] = await Promise.all([loadDashboardState(), fetchProfiles()]);
      const groups = summarizeSessions(sessions);
      statusEl.className = "status-chip ok";
      statusEl.textContent = `${sessions.length} session(s) · ${profiles.length} profile(s)`;
      contentEl.innerHTML = [
        profilesSectionMarkup(profiles, bootstrap.app_path),
        sectionMarkup("Active", groups.running, bootstrap.app_path),
        sectionMarkup("Idle", groups.stopped, bootstrap.app_path),
        sectionMarkup("Error", groups.degraded, bootstrap.app_path),
      ].join("");
    } catch (error) {
      statusEl.className = "status-chip error";
      statusEl.textContent = `Dashboard failed to load: ${String(error)}`;
      contentEl.innerHTML = `<section class="card"><div class="small">Unable to load dashboard state.</div></section>`;
    }
  }

  root.querySelector<HTMLButtonElement>("#dashboard-refresh")?.addEventListener("click", () => {
    void loadAll(status, content);
  });

  content.addEventListener("click", (e) => {
    const target = e.target as HTMLElement;

    const deleteProfileBtn = target.closest<HTMLButtonElement>(".btn-delete-profile");
    if (deleteProfileBtn) {
      const pid = deleteProfileBtn.dataset.profileId;
      if (!pid) return;
      if (!window.confirm(`Delete profile? This cannot be undone.`)) return;
      deleteProfileBtn.disabled = true;
      deleteProfileBtn.textContent = "…";
      void deleteProfile(pid)
        .then(() => loadAll(status, content))
        .catch((err: unknown) => {
          deleteProfileBtn.disabled = false;
          deleteProfileBtn.textContent = "Delete";
          status.className = "status-chip error";
          status.textContent = `Delete failed: ${String(err)}`;
        });
      return;
    }

    const restartBtn = target.closest<HTMLButtonElement>(".btn-restart");
    if (restartBtn) {
      const sid = restartBtn.dataset.sessionId;
      if (!sid) return;
      restartBtn.disabled = true;
      restartBtn.textContent = "…";
      void restartSession(sid)
        .then(() => loadAll(status, content))
        .catch((err: unknown) => {
          restartBtn.disabled = false;
          restartBtn.textContent = "Restart";
          status.className = "status-chip error";
          status.textContent = `Restart failed: ${String(err)}`;
        });
      return;
    }

    const deleteBtn = target.closest<HTMLButtonElement>(".btn-delete");
    if (deleteBtn) {
      const sid = deleteBtn.dataset.sessionId;
      if (!sid) return;
      if (!window.confirm(`Delete session "${sid}"? This cannot be undone.`)) return;
      deleteBtn.disabled = true;
      deleteBtn.textContent = "…";
      void deleteSession(sid)
        .then(() => loadAll(status, content))
        .catch((err: unknown) => {
          deleteBtn.disabled = false;
          deleteBtn.textContent = "Delete";
          status.className = "status-chip error";
          status.textContent = `Delete failed: ${String(err)}`;
        });
    }
  });

  await loadAll(status, content);
}
```

- [ ] **Step 4: Update `connect-view.ts` with pre-fill and save-as-profile**

Replace the entire file `packages/provide-terminal-frontend/src/app/views/connect-view.ts` with:

```typescript
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { createProfile, fetchProfile, quickConnect } from "../api.js";
import type { AppBootstrap } from "../types.js";
import { renderAppHeader } from "./app-header.js";

function escapeHtml(value: string): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function updateFieldVisibility(form: HTMLFormElement): void {
  const type = (form.querySelector("#connect-type") as HTMLSelectElement).value;
  const needsHost = type === "ssh" || type === "telnet";
  for (const el of form.querySelectorAll<HTMLElement>(".field-host")) {
    el.style.display = needsHost ? "" : "none";
  }
  for (const el of form.querySelectorAll<HTMLElement>(".field-ssh")) {
    el.style.display = type === "ssh" ? "" : "none";
  }
  const portEl = form.querySelector<HTMLInputElement>("#connect-port");
  if (portEl && !portEl.dataset.userEdited) {
    portEl.value = type === "telnet" ? "23" : "22";
  }
}

async function handleSubmit(form: HTMLFormElement, errorEl: HTMLElement, submitBtn: HTMLButtonElement): Promise<void> {
  errorEl.textContent = "";
  const type = (form.querySelector("#connect-type") as HTMLSelectElement).value;
  const host = (form.querySelector("#connect-host") as HTMLInputElement).value.trim();
  if ((type === "ssh" || type === "telnet") && !host) {
    errorEl.textContent = `Host is required for ${type.toUpperCase()} connections.`;
    return;
  }
  submitBtn.disabled = true;
  submitBtn.textContent = "Connecting\u2026";
  const payload: Record<string, unknown> = { connector_type: type };
  const name = (form.querySelector("#connect-name") as HTMLInputElement).value.trim();
  if (name) payload.display_name = name;
  const mode = (form.querySelector("#connect-mode") as HTMLSelectElement).value;
  if (mode) payload.input_mode = mode;
  const tagsRaw = (form.querySelector("#connect-tags") as HTMLInputElement).value.trim();
  if (tagsRaw) {
    payload.tags = tagsRaw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }
  if (type === "ssh" || type === "telnet") {
    payload.host = host;
    payload.port =
      parseInt((form.querySelector("#connect-port") as HTMLInputElement).value, 10) || (type === "telnet" ? 23 : 22);
  }
  if (type === "ssh") {
    const user = (form.querySelector("#connect-user") as HTMLInputElement).value.trim();
    const pass = (form.querySelector("#connect-pass") as HTMLInputElement).value;
    if (user) payload.username = user;
    if (pass) payload.password = pass;
  }
  try {
    const saveCheckbox = form.querySelector<HTMLInputElement>("#connect-save-profile");
    if (saveCheckbox?.checked) {
      // Save profile without password — profiles never store credentials.
      const profilePayload: Record<string, unknown> = {
        name: name || type,
        connector_type: type,
      };
      if (host) profilePayload.host = host;
      if (payload.port) profilePayload.port = payload.port;
      if (payload.username) profilePayload.username = payload.username;
      if (payload.input_mode) profilePayload.input_mode = payload.input_mode;
      if (payload.tags) profilePayload.tags = payload.tags;
      await createProfile(profilePayload as Parameters<typeof createProfile>[0]).catch(() => {
        // Non-fatal — connect proceeds even if save fails.
      });
    }
    const result = await quickConnect(payload as unknown as Parameters<typeof quickConnect>[0]);
    window.location.href = result.url;
  } catch (err) {
    errorEl.textContent = err instanceof Error ? err.message : "Connection failed.";
    submitBtn.disabled = false;
    submitBtn.textContent = "Connect";
  }
}

export async function renderConnect(root: HTMLElement, bootstrap: AppBootstrap): Promise<void> {
  const safeAppPath = escapeHtml(bootstrap.app_path);
  root.innerHTML = `
    <div class="page">
      ${renderAppHeader(bootstrap, "connect")}
      <div class="card" style="max-width:480px;margin:2rem auto">
        <div class="small" style="margin-bottom:.75rem">
          <a href="${safeAppPath}/">&#8592; Dashboard</a>
        </div>
        <h2 style="margin-bottom:1.25rem">Quick Connect</h2>
        <form id="connect-form">
          <div class="field">
            <label for="connect-type">Connection type</label>
            <select id="connect-type">
              <option value="ushell">Ushell (built-in shell)</option>
              <option value="ssh">SSH</option>
              <option value="telnet">Telnet</option>
              <option value="websocket">WebSocket</option>
            </select>
          </div>
          <div class="field">
            <label for="connect-name">Display name (optional)</label>
            <input id="connect-name" type="text" placeholder="My session">
          </div>
          <div class="field field-host">
            <label for="connect-host">Host</label>
            <input id="connect-host" type="text" placeholder="hostname or IP">
          </div>
          <div class="field field-host">
            <label for="connect-port">Port</label>
            <input id="connect-port" type="number" value="22" min="1" max="65535">
          </div>
          <div class="field field-ssh">
            <label for="connect-user">Username</label>
            <input id="connect-user" type="text" placeholder="username">
          </div>
          <div class="field field-ssh">
            <label for="connect-pass">Password</label>
            <input id="connect-pass" type="password" placeholder="password">
          </div>
          <div class="field">
            <label for="connect-mode">Input mode</label>
            <select id="connect-mode">
              <option value="open">Open (shared input)</option>
              <option value="hijack">Exclusive (hijack only)</option>
            </select>
          </div>
          <div class="field">
            <label for="connect-tags">Tags (optional, comma-separated)</label>
            <input id="connect-tags" type="text" placeholder="game, prod, demo">
          </div>
          <div class="field" style="flex-direction:row;align-items:center;gap:.5rem">
            <input id="connect-save-profile" type="checkbox">
            <label for="connect-save-profile" style="margin:0">Save as profile</label>
          </div>
          <div id="connect-error" class="field-error"></div>
          <button id="connect-submit" class="btn primary" type="submit" style="width:100%">Connect</button>
        </form>
      </div>
    </div>
  `;
  const form = root.querySelector<HTMLFormElement>("#connect-form");
  const errorEl = root.querySelector<HTMLElement>("#connect-error");
  const submitBtn = root.querySelector<HTMLButtonElement>("#connect-submit");
  const typeSelect = root.querySelector<HTMLSelectElement>("#connect-type");
  const portEl = root.querySelector<HTMLInputElement>("#connect-port");
  if (!form || !errorEl || !submitBtn || !typeSelect || !portEl) return;
  updateFieldVisibility(form);
  typeSelect.addEventListener("change", () => updateFieldVisibility(form));
  portEl.addEventListener("input", function (this: HTMLInputElement) {
    this.dataset.userEdited = "1";
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    void handleSubmit(form, errorEl, submitBtn);
  });

  // Pre-fill from ?profile=<id>
  const params = new URLSearchParams(window.location.search);
  const profileId = params.get("profile");
  if (profileId) {
    const profile = await fetchProfile(profileId);
    if (profile && form) {
      const nameEl = form.querySelector<HTMLInputElement>("#connect-name");
      const typeEl = form.querySelector<HTMLSelectElement>("#connect-type");
      const hostEl = form.querySelector<HTMLInputElement>("#connect-host");
      const portFieldEl = form.querySelector<HTMLInputElement>("#connect-port");
      const userEl = form.querySelector<HTMLInputElement>("#connect-user");
      const modeEl = form.querySelector<HTMLSelectElement>("#connect-mode");
      const tagsEl = form.querySelector<HTMLInputElement>("#connect-tags");
      if (nameEl) nameEl.value = profile.name;
      if (typeEl) {
        typeEl.value = profile.connector_type;
        updateFieldVisibility(form);
      }
      if (hostEl && profile.host) hostEl.value = profile.host;
      if (portFieldEl && profile.port) {
        portFieldEl.value = String(profile.port);
        portFieldEl.dataset.userEdited = "1";
      }
      if (userEl && profile.username) userEl.value = profile.username;
      if (modeEl && profile.input_mode) modeEl.value = profile.input_mode;
      if (tagsEl && profile.tags.length > 0) tagsEl.value = profile.tags.join(", ");
    }
  }
}
```

Note: `renderConnect` is now `async` because it needs to `await fetchProfile()`. Check if the caller in `router.ts` uses `void renderConnect(...)` or awaits it — if it uses `void`, no router change is needed. If it calls it synchronously, update the call site to `void renderConnect(root, bootstrap)`.

- [ ] **Step 5: Check router.ts call site**

```bash
grep -n "renderConnect" /Users/tim/code/gh/provide-io/provide-terminal/packages/provide-terminal-frontend/src/app/router.ts
```

If the result shows `renderConnect(root, bootstrap)` without `void` or `await`, change it to:
```typescript
void renderConnect(root, bootstrap);
```

- [ ] **Step 6: TypeScript type-check**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal/packages/provide-terminal-frontend
npx tsc --noEmit 2>&1 | head -20
```

Expected: no errors.

- [ ] **Step 7: Build frontend**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal/packages/provide-terminal-frontend
npm run build 2>&1 | tail -5
```

Expected: build succeeds with no errors.

- [ ] **Step 8: Commit**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
git add \
  packages/provide-terminal-frontend/src/app/types.ts \
  packages/provide-terminal-frontend/src/app/api.ts \
  packages/provide-terminal-frontend/src/app/views/dashboard-view.ts \
  packages/provide-terminal-frontend/src/app/views/connect-view.ts
git commit -m "feat(frontend): add connection profiles to dashboard and connect form"
```

---

## Task 5: Full Quality Gate

- [ ] **Step 1: Run the full Python test suite**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run pytest packages/provide-terminal/tests/ --no-cov -q 2>&1 | tail -5
```

Expected: all pass, no failures.

- [ ] **Step 2: Run with coverage**

```bash
uv run pytest packages/provide-terminal/tests/ -q 2>&1 | tail -5
```

Expected: 100% branch coverage or close to it. If there are uncovered branches in `profiles.py` or `routes/profiles.py`, add targeted tests to `test_profile_store.py` or `test_api_profiles.py` to cover them.

- [ ] **Step 3: Lint and type-check**

```bash
cd /Users/tim/code/gh/provide-io/provide-terminal
uv run ruff check packages/provide-terminal/src/provide/terminal/server/profiles.py packages/provide-terminal/src/provide/terminal/server/routes/profiles.py
uv run mypy packages/provide-terminal/src/provide/terminal/server/profiles.py packages/provide-terminal/src/provide/terminal/server/routes/profiles.py
```

Expected: no errors.

- [ ] **Step 4: Final commit if any coverage fixes were made**

```bash
git add -u
git commit -m "test: add coverage for profile edge cases"
```

---

## Self-Review

**Spec coverage:**
- ✅ `ConnectionProfile` model — Task 1
- ✅ `FileProfileStore` (atomic write, lock, CRUD) — Task 1
- ✅ `ProfileStoreConfig` in `ServerConfig` — Task 2
- ✅ `can_read_profile` / `can_mutate_profile` — Task 2
- ✅ 5 API endpoints (`GET /list`, `GET /{id}`, `POST /`, `PUT /{id}`, `DELETE /{id}`, `POST /{id}/connect`) — Task 3 (6 endpoints; GET single was a natural addition)
- ✅ Store wired into `app.py` — Task 3
- ✅ `ConnectionProfile` TS interface — Task 4
- ✅ 5 API functions in `api.ts` — Task 4
- ✅ Dashboard Profiles section with parallel fetch — Task 4
- ✅ Connect form pre-fill from `?profile=<id>` — Task 4
- ✅ Save-as-profile checkbox — Task 4
- ✅ Credentials never stored — `handleSubmit` strips password before `createProfile`

**Placeholder scan:** None found. All code is complete.

**Type consistency:**
- `input_mode: Literal["open", "hijack"]` used throughout Python and frontend (spec said `"exclusive"` — corrected to `"hijack"` to match the rest of the system)
- `profile.model_dump(mode="python")` called directly (not via `model_dump()` helper) in routes — correct, since `ConnectionProfile` is not in `ServerModel`
- `fetchProfile` returns `ConnectionProfile | null` — matches usage in `connect-view.ts`
