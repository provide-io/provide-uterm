#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""CF SqliteStateStore resume token CRUD + ws_routes resume dispatch tests."""

from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from provide_terminal_cloudflare.api.ws_routes import handle_socket_message
from provide_terminal_cloudflare.bridge.hijack import HijackCoordinator
from provide_terminal_cloudflare.state.store import SqliteStateStore


@pytest.fixture()
def store() -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    s = SqliteStateStore(conn.execute)
    s.migrate()
    return s


class TestResumeTokenCRUD:
    def test_create_and_get(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "admin", 60)
        record = store.get_resume_token("tok1")
        assert record is not None
        assert record["token"] == "tok1"
        assert record["worker_id"] == "w1"
        assert record["role"] == "admin"
        assert record["was_hijack_owner"] is False

    def test_get_nonexistent_returns_none(self, store: SqliteStateStore) -> None:
        assert store.get_resume_token("nonexistent") is None

    def test_get_expired_returns_none(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "admin", 0.001)
        time.sleep(0.02)
        assert store.get_resume_token("tok1") is None

    def test_mark_hijack_owner(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "admin", 60)
        store.mark_resume_hijack_owner("tok1", True)
        record = store.get_resume_token("tok1")
        assert record is not None
        assert record["was_hijack_owner"] is True

    def test_mark_hijack_owner_false(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "admin", 60)
        store.mark_resume_hijack_owner("tok1", True)
        store.mark_resume_hijack_owner("tok1", False)
        record = store.get_resume_token("tok1")
        assert record is not None
        assert record["was_hijack_owner"] is False

    def test_revoke(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "admin", 60)
        store.revoke_resume_token("tok1")
        assert store.get_resume_token("tok1") is None

    def test_revoke_nonexistent_is_noop(self, store: SqliteStateStore) -> None:
        store.revoke_resume_token("nonexistent")  # should not raise

    def test_cleanup_expired(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "admin", 0.001)
        store.create_resume_token("tok2", "w2", "viewer", 60)
        time.sleep(0.02)
        store.cleanup_expired_tokens()
        assert store.get_resume_token("tok1") is None
        assert store.get_resume_token("tok2") is not None

    def test_multiple_tokens_same_worker(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "admin", 60)
        store.create_resume_token("tok2", "w1", "viewer", 60)
        r1 = store.get_resume_token("tok1")
        r2 = store.get_resume_token("tok2")
        assert r1 is not None and r1["role"] == "admin"
        assert r2 is not None and r2["role"] == "viewer"

    def test_role_default(self, store: SqliteStateStore) -> None:
        store.create_resume_token("tok1", "w1", "operator", 60)
        record = store.get_resume_token("tok1")
        assert record is not None
        assert record["role"] == "operator"

    def test_expires_at_set_correctly(self, store: SqliteStateStore) -> None:
        before = time.time()
        store.create_resume_token("tok1", "w1", "admin", 300)
        record = store.get_resume_token("tok1")
        assert record is not None
        assert record["expires_at"] >= before + 300
        assert record["expires_at"] <= time.time() + 300 + 1


# ---------------------------------------------------------------------------
# ws_routes _handle_resume integration tests
# ---------------------------------------------------------------------------


class _MockWs:
    """Minimal mock WS that supports serializeAttachment."""

    def __init__(self) -> None:
        self._attachment: str | None = None

    def serializeAttachment(self, val: str) -> None:  # noqa: N802
        self._attachment = val


class _MockRuntime:
    """Minimal runtime mock for ws_routes resume testing."""

    def __init__(self, store: SqliteStateStore) -> None:
        self.worker_id = "w1"
        self.input_mode = "hijack"
        self.hijack = HijackCoordinator()
        self.last_snapshot: dict | None = None
        self.last_analysis: str | None = None
        self.browser_hijack_owner: dict[str, str] = {}
        self.worker_ws = None
        self._sent: list[dict] = []
        self.config = SimpleNamespace(
            limits=SimpleNamespace(max_ws_message_bytes=1_048_576, max_input_chars=10_000),
            resume_ttl_s=300,
        )
        self.store = store

    async def send_ws(self, ws: object, frame: dict) -> None:
        self._sent.append(frame)

    async def send_hijack_state(self, ws: object) -> None:
        self._sent.append({"type": "hijack_state"})

    async def broadcast_worker_frame(self, frame: object) -> None:
        pass

    async def push_worker_input(self, data: str) -> bool:
        return True

    def ws_key(self, ws: object) -> str:
        return str(id(ws))

    def _socket_browser_role(self, ws: object) -> str:
        return "admin"


class TestWsRoutesResume:
    @pytest.fixture()
    def runtime(self, store: SqliteStateStore) -> _MockRuntime:
        return _MockRuntime(store)

    async def test_resume_valid_token(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume token → updated hello with resumed=True."""
        store.create_resume_token("tok-abc", "w1", "admin", 300)
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": "tok-abc"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        # Should have sent: hello + hijack_state
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        assert hellos[0]["resumed"] is True
        assert hellos[0]["resume_token"] is not None
        assert hellos[0]["role"] == "admin"
        # Old token should be revoked
        assert store.get_resume_token("tok-abc") is None

    async def test_resume_expired_token_ignored(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Expired token → silently ignored, no hello sent."""
        store.create_resume_token("tok-exp", "w1", "admin", 0.001)
        time.sleep(0.02)
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": "tok-exp"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 0

    async def test_resume_wrong_worker_ignored(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Token from different worker → silently ignored."""
        store.create_resume_token("tok-wrong", "other-worker", "admin", 300)
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": "tok-wrong"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 0

    async def test_resume_empty_token_ignored(self, runtime: _MockRuntime) -> None:
        """Empty token → silently ignored."""
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": ""})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        assert len(runtime._sent) == 0

    async def test_resume_nonexistent_token_ignored(self, runtime: _MockRuntime) -> None:
        """Nonexistent token → silently ignored."""
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": "nonexistent"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        assert len(runtime._sent) == 0

    async def test_resume_updates_attachment(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume should update ws attachment with restored role."""
        store.create_resume_token("tok-attach", "w1", "operator", 300)
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": "tok-attach"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        assert ws._attachment == "browser:operator:w1"
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert hellos[0]["role"] == "operator"

    async def test_resume_sends_snapshot_if_available(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume with existing snapshot → snapshot sent after hello."""
        runtime.last_snapshot = {"type": "snapshot", "screen": "test"}
        store.create_resume_token("tok-snap", "w1", "admin", 300)
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": "tok-snap"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        # Should have: hello, hijack_state, snapshot
        types = [m.get("type") for m in runtime._sent]
        assert "hello" in types
        assert "hijack_state" in types
        assert "snapshot" in types

    async def test_resume_serialize_attachment_failure(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """serializeAttachment failure should not break resume flow."""
        store.create_resume_token("tok-fail", "w1", "admin", 300)

        class _BrokenWs:
            def serializeAttachment(self, val: str) -> None:  # noqa: N802
                raise RuntimeError("attachment failed")

        ws = _BrokenWs()
        raw = json.dumps({"type": "resume", "token": "tok-fail"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        assert hellos[0]["resumed"] is True

    async def test_resume_issues_new_token(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume → new token created in store."""
        store.create_resume_token("tok-new", "w1", "admin", 300)
        ws = _MockWs()
        raw = json.dumps({"type": "resume", "token": "tok-new"})
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        new_token = hellos[0]["resume_token"]
        assert new_token != "tok-new"
        # New token should be valid in the store
        record = store.get_resume_token(new_token)
        assert record is not None
        assert record["worker_id"] == "w1"
