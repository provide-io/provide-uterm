#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""CF SqliteStateStore resume token CRUD + ws_routes resume dispatch tests."""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace

import pytest
from provide.terminal.cloudflare.api.ws_routes import handle_socket_message
from provide.terminal.cloudflare.bridge.hijack import HijackCoordinator
from provide.terminal.cloudflare.contracts import frame_json
from provide.terminal.cloudflare.state.store import SqliteStateStore


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
        self.current_role = "admin"

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
        return self.current_role


class TestWsRoutesResume:
    @pytest.fixture()
    def runtime(self, store: SqliteStateStore) -> _MockRuntime:
        return _MockRuntime(store)

    async def test_resume_valid_token(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume token → updated hello with resumed=True."""
        store.create_resume_token("tok-abc", "w1", "admin", 300)
        ws = _MockWs()
        raw = frame_json("resume", token="tok-abc")
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
        raw = frame_json("resume", token="tok-exp")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 0

    async def test_resume_wrong_worker_ignored(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Token from different worker → silently ignored."""
        store.create_resume_token("tok-wrong", "other-worker", "admin", 300)
        ws = _MockWs()
        raw = frame_json("resume", token="tok-wrong")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 0

    async def test_resume_empty_token_ignored(self, runtime: _MockRuntime) -> None:
        """Empty token → silently ignored."""
        ws = _MockWs()
        raw = frame_json("resume", token="")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        assert len(runtime._sent) == 0

    async def test_resume_nonexistent_token_ignored(self, runtime: _MockRuntime) -> None:
        """Nonexistent token → silently ignored."""
        ws = _MockWs()
        raw = frame_json("resume", token="nonexistent")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        assert len(runtime._sent) == 0

    async def test_resume_updates_attachment(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume should update ws attachment with restored role."""
        store.create_resume_token("tok-attach", "w1", "operator", 300)
        ws = _MockWs()
        raw = frame_json("resume", token="tok-attach")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        assert ws._attachment == "browser:operator:w1"
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert hellos[0]["role"] == "operator"

    async def test_resume_sends_snapshot_if_available(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume with existing snapshot → snapshot sent after hello."""
        runtime.last_snapshot = {"type": "snapshot", "screen": "test"}
        store.create_resume_token("tok-snap", "w1", "admin", 300)
        ws = _MockWs()
        raw = frame_json("resume", token="tok-snap")
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
        raw = frame_json("resume", token="tok-fail")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        assert hellos[0]["resumed"] is True

    async def test_resume_issues_new_token(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Valid resume → new token created in store."""
        store.create_resume_token("tok-new", "w1", "admin", 300)
        ws = _MockWs()
        raw = frame_json("resume", token="tok-new")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        new_token = hellos[0]["resume_token"]
        assert new_token != "tok-new"
        # New token should be valid in the store
        record = store.get_resume_token(new_token)
        assert record is not None
        assert record["worker_id"] == "w1"

    async def test_resume_does_not_escalate_above_current_role(
        self, runtime: _MockRuntime, store: SqliteStateStore
    ) -> None:
        """Stored roles must not elevate beyond the current socket role."""
        runtime.current_role = "viewer"
        store.create_resume_token("tok-elev", "w1", "admin", 300)
        ws = _MockWs()
        raw = frame_json("resume", token="tok-elev")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        assert hellos[0]["role"] == "viewer"
        assert hellos[0]["can_hijack"] is False
        assert ws._attachment == "browser:viewer:w1"
        record = store.get_resume_token(hellos[0]["resume_token"])
        assert record is not None
        assert record["role"] == "viewer"

    async def test_resume_preserves_lower_stored_role(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """A lower stored role should survive resume when current auth is broader."""
        runtime.current_role = "admin"
        store.create_resume_token("tok-op", "w1", "operator", 300)
        ws = _MockWs()
        raw = frame_json("resume", token="tok-op")
        await handle_socket_message(runtime, ws, raw, is_worker=False)
        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        assert hellos[0]["role"] == "operator"
        assert hellos[0]["can_hijack"] is False
        assert ws._attachment == "browser:operator:w1"

    async def test_resume_reclaims_hijack_when_was_owner(self, runtime: _MockRuntime, store: SqliteStateStore) -> None:
        """Lines 128-137: hijack ownership is reclaimed when stored token marks was_hijack_owner=True."""
        store.create_resume_token("tok-hijack", "w1", "admin", 300)
        store.mark_resume_hijack_owner("tok-hijack", True)
        runtime.current_role = "admin"
        runtime.input_mode = "hijack"

        # Patch runtime with persist_lease and broadcast_hijack_state
        persisted: list = []
        broadcast_calls: list = []

        async def _broadcast_hijack_state() -> None:
            broadcast_calls.append(True)

        runtime.persist_lease = lambda session: persisted.append(session)
        runtime.broadcast_hijack_state = _broadcast_hijack_state
        runtime.push_worker_control = SimpleNamespace()  # replaced below

        control_calls: list = []

        async def _push_worker_control(action: str, *, owner: str, lease_s: int) -> bool:
            control_calls.append(action)
            return True

        runtime.push_worker_control = _push_worker_control  # type: ignore[assignment]

        ws = _MockWs()
        raw = frame_json("resume", token="tok-hijack")
        await handle_socket_message(runtime, ws, raw, is_worker=False)

        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        assert hellos[0]["role"] == "admin"
        # Hijack should have been acquired → persist_lease and broadcast_hijack_state called
        assert persisted
        assert broadcast_calls

    async def test_resume_hijack_reclaim_skipped_when_acquire_fails(
        self, runtime: _MockRuntime, store: SqliteStateStore
    ) -> None:
        """Line 130->140: hijack acquire returns ok=False → reclaim skipped, resume still succeeds."""
        from provide.terminal.cloudflare.bridge.hijack import HijackSession

        store.create_resume_token("tok-fail-acq", "w1", "admin", 300)
        store.mark_resume_hijack_owner("tok-fail-acq", True)
        runtime.current_role = "admin"
        runtime.input_mode = "hijack"

        # Pre-acquire with a different owner so acquire("dashboard_resume", ...) returns ok=False
        other_session = HijackSession(hijack_id="other-id", owner="other_owner", lease_expires_at=time.time() + 60)
        runtime.hijack._session = other_session

        runtime.persist_lease = lambda session: None
        broadcast_calls: list = []

        async def _broadcast_hijack_state() -> None:
            broadcast_calls.append(True)

        runtime.broadcast_hijack_state = _broadcast_hijack_state

        async def _push_worker_control(action: str, *, owner: str, lease_s: int) -> bool:
            return True

        runtime.push_worker_control = _push_worker_control  # type: ignore[assignment]

        ws = _MockWs()
        raw = frame_json("resume", token="tok-fail-acq")
        await handle_socket_message(runtime, ws, raw, is_worker=False)

        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        # No hijack reclaim occurred
        assert not broadcast_calls

    async def test_resume_hijack_reclaim_renewal_skips_pause(
        self, runtime: _MockRuntime, store: SqliteStateStore
    ) -> None:
        """Line 134->136: is_renewal=True → push_worker_control('pause') not called."""
        import time as _time

        from provide.terminal.cloudflare.bridge.hijack import HijackSession

        store.create_resume_token("tok-renew", "w1", "admin", 300)
        store.mark_resume_hijack_owner("tok-renew", True)
        runtime.current_role = "admin"
        runtime.input_mode = "hijack"

        # Pre-acquire with the same "dashboard_resume" owner so it's a renewal
        existing = HijackSession(hijack_id="existing-id", owner="dashboard_resume", lease_expires_at=_time.time() + 60)
        runtime.hijack._session = existing

        persisted: list = []
        runtime.persist_lease = lambda session: persisted.append(session)
        broadcast_calls: list = []

        async def _broadcast_hijack_state() -> None:
            broadcast_calls.append(True)

        runtime.broadcast_hijack_state = _broadcast_hijack_state
        control_calls: list = []

        async def _push_worker_control(action: str, *, owner: str, lease_s: int) -> bool:
            control_calls.append(action)
            return True

        runtime.push_worker_control = _push_worker_control  # type: ignore[assignment]

        ws = _MockWs()
        raw = frame_json("resume", token="tok-renew")
        await handle_socket_message(runtime, ws, raw, is_worker=False)

        hellos = [m for m in runtime._sent if m.get("type") == "hello"]
        assert len(hellos) == 1
        assert hellos[0]["role"] == "admin"
        # Hijack reclaimed (renewal) but pause not sent
        assert persisted
        assert broadcast_calls
        assert control_calls == []  # no pause because is_renewal=True
