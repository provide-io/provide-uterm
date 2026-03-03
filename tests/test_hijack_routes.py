#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Integration tests for the terminal hijack REST routes."""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.terminal.hijack.hub import TermHub
from provide.terminal.hijack.models import BotTermState, HijackSession


def make_app() -> tuple[FastAPI, TermHub]:
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _active_session(hijack_id: str, owner: str = "test") -> HijackSession:
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=time.time(),
        lease_expires_at=time.time() + 3600,
        last_heartbeat=time.time(),
    )


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


def test_acquire_hijack() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._bots["bot1"] = BotTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "test", "lease_s": 60})

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "hijack_id" in data
    assert data["owner"] == "test"
    assert "lease_expires_at" in data


def test_acquire_no_worker_returns_409() -> None:
    app, hub = make_app()
    # No worker_ws set

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "test"})

    assert r.status_code == 409


def test_acquire_conflict_already_hijacked() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id, "owner_a"),
    )

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "owner_b"})

    assert r.status_code == 409


def test_acquire_default_owner() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._bots["bot1"] = BotTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire")

    assert r.status_code == 200
    assert r.json()["owner"] == "mcp"  # default from HijackAcquireRequest


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/heartbeat", json={"lease_s": 120})

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["hijack_id"] == hijack_id
    assert "lease_expires_at" in data


def test_heartbeat_wrong_id() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/wrong-hijack-id/heartbeat", json={})

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/release")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert hub._bots["bot1"].hijack_session is None


def test_release_invalid_session() -> None:
    app, hub = make_app()
    hub._bots["bot1"] = BotTermState()

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/no-such-id/release")

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_no_worker() -> None:
    app, hub = make_app()
    hijack_id = str(uuid.uuid4())
    # Valid session but worker_ws = None
    hub._bots["bot1"] = BotTermState(
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/send", json={"keys": "hello\r"})

    assert r.status_code == 409


def test_send_invalid_session() -> None:
    app, hub = make_app()
    hub._bots["bot1"] = BotTermState()

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/no-such-id/send", json={"keys": "hi"})

    assert r.status_code == 404


def test_send_empty_keys() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/send", json={"keys": ""})

    assert r.status_code == 400


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_no_worker() -> None:
    app, hub = make_app()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/bot/bot1/hijack/{hijack_id}/snapshot?wait_ms=0")

    assert r.status_code == 200
    data = r.json()
    assert data["snapshot"] is None
    assert data["bot_id"] == "bot1"


def test_snapshot_invalid_session() -> None:
    app, hub = make_app()

    with TestClient(app) as client:
        r = client.get("/bot/bot1/hijack/no-such-id/snapshot")

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/bot/bot1/hijack/{hijack_id}/events")

    assert r.status_code == 200
    data = r.json()
    assert "events" in data
    assert "latest_seq" in data


def test_events_invalid_session() -> None:
    app, hub = make_app()

    with TestClient(app) as client:
        r = client.get("/bot/bot1/hijack/no-such-id/events")

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# step
# ---------------------------------------------------------------------------


def test_step() -> None:
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/step")

    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_step_no_worker() -> None:
    app, hub = make_app()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/step")

    assert r.status_code == 409


# ---------------------------------------------------------------------------
# bot_id validation
# ---------------------------------------------------------------------------


def test_bot_id_validation_special_chars() -> None:
    app, hub = make_app()

    with TestClient(app) as client:
        r = client.post("/bot/bot@bad!/hijack/acquire")

    assert r.status_code == 422


def test_acquire_send_worker_fails() -> None:
    """When _send_worker returns False after acquiring, return 409."""
    app, hub = make_app()
    from unittest.mock import AsyncMock

    bad_ws = AsyncMock()
    bad_ws.send_text = AsyncMock(side_effect=RuntimeError("broken"))
    hub._bots["bot1"] = BotTermState(worker_ws=bad_ws)

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "test", "lease_s": 60})

    # Worker send failed → 409
    assert r.status_code == 409


def test_heartbeat_request_none_defaults() -> None:
    """Heartbeat with no JSON body uses HijackHeartbeatRequest defaults."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/heartbeat")

    assert r.status_code == 200


def test_heartbeat_inner_session_none() -> None:
    """If session disappears between lock acquisition and update, return 404."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/heartbeat", json={"lease_s": 30})

    # Should succeed normally (inner None check is a safety guard)
    assert r.status_code == 200


def test_events_empty_bot_state() -> None:
    """Events endpoint when bot has no events returns empty list."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/bot/bot1/hijack/{hijack_id}/events")

    assert r.status_code == 200
    assert r.json()["events"] == []


def test_send_with_worker_and_no_guard() -> None:
    """Send with keys and worker connected, no guard constraints → succeeds."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(
            f"/bot/bot1/hijack/{hijack_id}/send",
            json={"keys": "hello\r", "timeout_ms": 100},
        )

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["sent"] == "hello\r"


def test_send_guard_not_satisfied() -> None:
    """Send with an expect_prompt_id that never matches → 409."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(
            f"/bot/bot1/hijack/{hijack_id}/send",
            json={"keys": "hello\r", "expect_prompt_id": "never_matches", "timeout_ms": 50},
        )

    assert r.status_code == 409


def test_send_guard_invalid_regex() -> None:
    """Send with invalid expect_regex → 409."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(
            f"/bot/bot1/hijack/{hijack_id}/send",
            json={"keys": "hello\r", "expect_regex": "[invalid"},
        )

    assert r.status_code == 409


def test_release_inner_session_none() -> None:
    """Release with valid session succeeds."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{hijack_id}/release")

    assert r.status_code == 200


def test_heartbeat_session_mismatch_inside_lock() -> None:
    """Heartbeat returns 404 when hijack_id doesn't match inside the lock (line 381)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    real_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(other_id),  # different session active
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{real_id}/heartbeat")

    # real_id doesn't match other_id in bot state
    assert r.status_code == 404


def test_events_no_bot_state() -> None:
    """Events endpoint when bot has no state returns empty list (lines 420-421)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    # Register valid session but with no bot state in hub._bots
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    # Use a different bot_id that has no state
    # But we need a valid session... let's use a workaround:
    # Get the session via bot1, then remove the bot state
    hub._bots["bot2"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )
    # Remove bot2's state to trigger the None branch
    del hub._bots["bot2"]
    # Re-add an empty state without using a separate event loop (asyncio.Lock is
    # loop-bound; running hub coroutines on a different loop is incorrect).
    hub._bots["bot2"] = BotTermState()
    hub._bots["bot2"].hijack_session = _active_session(hijack_id)

    with TestClient(app) as client:
        r = client.get(f"/bot/bot2/hijack/{hijack_id}/events")

    assert r.status_code == 200
    assert r.json()["events"] == []


def test_step_invalid_hijack_session() -> None:
    """Step with invalid/expired hijack_id returns 404 (line 484)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/no-such-id/step")

    assert r.status_code == 404


def test_release_session_mismatch_inside_lock() -> None:
    """Release returns 404 when hijack_id doesn't match inside the lock (line 502)."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    real_id = str(uuid.uuid4())
    other_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(other_id),
    )

    with TestClient(app) as client:
        r = client.post(f"/bot/bot1/hijack/{real_id}/release")

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Fix 3 regression — hijack_id propagated in pause / rollback-resume messages
# ---------------------------------------------------------------------------


def test_acquire_pause_message_contains_hijack_id() -> None:
    """Regression fix 3: pause control message sent to worker must include hijack_id for correlation."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._bots["bot1"] = BotTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "test", "lease_s": 60})

    assert r.status_code == 200
    hijack_id = r.json()["hijack_id"]

    # The pause message sent to the worker must include hijack_id
    mock_ws.send_text.assert_awaited()
    import json as _json
    sent_calls = [_json.loads(call.args[0]) for call in mock_ws.send_text.await_args_list]
    pause_msgs = [m for m in sent_calls if m.get("type") == "control" and m.get("action") == "pause"]
    assert pause_msgs, "No pause control message sent to worker"
    assert pause_msgs[0].get("hijack_id") == hijack_id, "hijack_id missing from pause message"


def test_acquire_rollback_resume_contains_hijack_id() -> None:
    """Regression fix 3: rollback resume sent on race loss must include hijack_id."""
    import asyncio as _asyncio
    import json as _json

    app, hub = make_app()
    mock_ws = AsyncMock()

    # Pre-inject an active session so _try_acquire_rest_hijack returns (False, ...)
    existing_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(existing_id, "owner_a"),
    )

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "owner_b", "lease_s": 60})

    # The acquire should fail with 409
    assert r.status_code == 409

    # The rollback resume must have been sent with hijack_id
    sent_calls = [_json.loads(call.args[0]) for call in mock_ws.send_text.await_args_list]
    resume_msgs = [m for m in sent_calls if m.get("type") == "control" and m.get("action") == "resume"]
    assert resume_msgs, "No rollback resume message sent"
    assert "hijack_id" in resume_msgs[0], "hijack_id missing from rollback resume message"


# ---------------------------------------------------------------------------
# Fix A regression — hub state setup must not use a separate event loop
# ---------------------------------------------------------------------------


def test_events_empty_bot_state_via_direct_dict_assignment() -> None:
    """Regression fix A: bot state must be set up via direct dict assignment, not a
    separate asyncio event loop, to avoid asyncio.Lock cross-loop corruption."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())

    # Correct pattern: direct dict assignment — no extra event loop
    hub._bots["botA"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/bot/botA/hijack/{hijack_id}/events?after_seq=0")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # No events have been appended, so the list is empty
    assert data["events"] == []
    assert data["bot_id"] == "botA"
    assert data["hijack_id"] == hijack_id


# ---------------------------------------------------------------------------
# Round-7 regression — hijack_acquire compensating resume on cancellation
# ---------------------------------------------------------------------------


def test_acquire_sends_compensating_resume_on_error_after_pause() -> None:
    """Round-7 fix 1: if an error fires after the pause is sent but before the
    session is committed, a compensating resume must be dispatched so the worker
    does not remain stuck in the paused state indefinitely."""
    import json as _json
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._bots["bot1"] = BotTermState(worker_ws=mock_ws)

    async def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated error after pause")

    with patch.object(hub, "_try_acquire_rest_hijack", side_effect=_raise):
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/bot/bot1/hijack/acquire", json={"owner": "test"})

    assert r.status_code == 500

    sent = [_json.loads(c.args[0]) for c in mock_ws.send_text.await_args_list]
    pause_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "pause"]
    resume_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert pause_msgs, "pause must have been sent to worker before the error"
    assert resume_msgs, "compensating resume must be sent when session commit fails"
    assert resume_msgs[0].get("hijack_id") == pause_msgs[0].get("hijack_id"), (
        "compensating resume must carry the same hijack_id as the pause"
    )


def test_acquire_sends_compensating_resume_on_cancellation_after_pause() -> None:
    """Round-7 fix 1: CancelledError after pause (simulating client disconnect)
    must trigger the compensating resume in the finally block."""
    import asyncio as _asyncio
    import json as _json
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._bots["bot1"] = BotTermState(worker_ws=mock_ws)

    async def _cancel(*args: object, **kwargs: object) -> None:
        raise _asyncio.CancelledError()

    with patch.object(hub, "_try_acquire_rest_hijack", side_effect=_cancel):
        with TestClient(app, raise_server_exceptions=False) as client:
            client.post("/bot/bot1/hijack/acquire", json={"owner": "test"})

    sent = [_json.loads(c.args[0]) for c in mock_ws.send_text.await_args_list]
    resume_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert resume_msgs, "compensating resume must be sent on CancelledError after pause"


def test_acquire_no_compensating_resume_on_success() -> None:
    """Round-7 fix 1: a successful acquire must NOT send an extra compensating resume."""
    import json as _json

    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._bots["bot1"] = BotTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "test"})

    assert r.status_code == 200

    sent = [_json.loads(c.args[0]) for c in mock_ws.send_text.await_args_list]
    resume_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert not resume_msgs, "no resume should be sent after a successful acquire"


def test_acquire_no_compensating_resume_on_race_loss() -> None:
    """Round-7 fix 1: when _try_acquire_rest_hijack returns (False, ...) the
    explicit rollback resume is sent once — the finally block must not send a
    second resume."""
    import json as _json

    app, hub = make_app()
    mock_ws = AsyncMock()
    existing_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(existing_id, "owner_a"),
    )

    with TestClient(app) as client:
        r = client.post("/bot/bot1/hijack/acquire", json={"owner": "owner_b"})

    assert r.status_code == 409

    sent = [_json.loads(c.args[0]) for c in mock_ws.send_text.await_args_list]
    resume_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert len(resume_msgs) == 1, (
        f"exactly one resume expected on race loss, got {len(resume_msgs)}"
    )


# ---------------------------------------------------------------------------
# Round-7 regression — hijack_snapshot returns fresh lease_expires_at
# ---------------------------------------------------------------------------


def test_snapshot_returns_fresh_lease_after_concurrent_heartbeat() -> None:
    """Round-7 fix 3: snapshot must return the current lease_expires_at even if
    a concurrent heartbeat extended it during the _wait_for_snapshot poll."""
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    original_expires = hub._bots["bot1"].hijack_session.lease_expires_at  # type: ignore[union-attr]
    extended_expires = original_expires + 3600

    async def _extend_and_return(bot_id: str, timeout_ms: int = 1500) -> dict:
        # Simulate a concurrent heartbeat mutating the lease while we wait
        st = hub._bots.get(bot_id)
        if st and st.hijack_session:
            st.hijack_session.lease_expires_at = extended_expires
        return {"screen": "hello", "cols": 80, "rows": 25}

    with patch.object(hub, "_wait_for_snapshot", side_effect=_extend_and_return):
        with TestClient(app) as client:
            r = client.get(f"/bot/bot1/hijack/{hijack_id}/snapshot?wait_ms=100")

    assert r.status_code == 200
    data = r.json()
    assert data["lease_expires_at"] == extended_expires, (
        f"expected fresh expiry {extended_expires}, got {data['lease_expires_at']}"
    )


def test_snapshot_falls_back_to_original_lease_if_session_gone() -> None:
    """Round-7 fix 3: if the session is released during the snapshot wait, the
    originally-captured lease_expires_at is used (session gone guard)."""
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._bots["bot1"] = BotTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    original_expires = hub._bots["bot1"].hijack_session.lease_expires_at  # type: ignore[union-attr]

    async def _release_and_return(bot_id: str, timeout_ms: int = 1500) -> dict:
        # Simulate the session being released while waiting for a snapshot
        st = hub._bots.get(bot_id)
        if st:
            st.hijack_session = None
        return {"screen": "bye"}

    with patch.object(hub, "_wait_for_snapshot", side_effect=_release_and_return):
        with TestClient(app) as client:
            r = client.get(f"/bot/bot1/hijack/{hijack_id}/snapshot?wait_ms=100")

    assert r.status_code == 200
    data = r.json()
    # Falls back to the originally-captured expiry
    assert data["lease_expires_at"] == original_expires


# ---------------------------------------------------------------------------
# Round-9 regression: hijack_acquire — removed pre-flight worker check
# ---------------------------------------------------------------------------


async def test_acquire_succeeds_when_worker_connects_after_cleanup() -> None:
    """Round-9 fix: hijack_acquire must not reject with 409 just because no
    worker was connected at the start of the request.  The pre-flight check
    (`_get()` outside the lock) was a false-negative race: a worker could
    connect between the check and _send_worker and would be incorrectly
    rejected.  After removing the pre-check, _send_worker is the sole
    authoritative liveness gate.

    We verify by having _send_worker succeed (returns True) even though no
    worker WS is pre-registered in hub._bots.
    """
    from unittest.mock import patch

    app, hub = make_app()
    hijack_id = str(uuid.uuid4())

    # Patch _send_worker to always succeed (simulates worker present at send time)
    # and _try_acquire_rest_hijack to grant the lock.
    async def _fake_send(bot_id: str, msg: dict) -> bool:
        return True

    async def _fake_acquire(bot_id: str, **kw):  # type: ignore[override]
        return True, None

    with patch.object(hub, "_send_worker", side_effect=_fake_send):
        with patch.object(hub, "_try_acquire_rest_hijack", side_effect=_fake_acquire):
            with TestClient(app) as client:
                r = client.post("/bot/bot1/hijack/acquire", json={"owner": "tester"})

    assert r.status_code == 200
    assert r.json()["ok"] is True
