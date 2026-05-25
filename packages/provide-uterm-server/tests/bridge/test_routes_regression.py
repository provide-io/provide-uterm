#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression tests for the terminal hijack REST routes.

Covers: Round-3 hijack ID propagation, Fix-A event loop safety, Round-7
compensating resume on cancellation, Round-7 snapshot lease, and Round-9
worker connect race.  Snapshot/events/step/send-guard tests live in
test_hijack_routes_advanced.py.
"""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from provide.uterm.bridge.hub import TermHub
from provide.uterm.bridge.models import HijackSession, WorkerTermState
from tests.bridge.control_channel_helpers import decode_control_payloads


def make_app() -> tuple[FastAPI, TermHub]:
    hub = TermHub()
    app = FastAPI()
    app.include_router(hub.create_router())
    return app, hub


def _active_session(hijack_id: str, owner: str = "test") -> HijackSession:
    return HijackSession(
        hijack_id=hijack_id,
        owner=owner,
        acquired_at=time.monotonic(),
        lease_expires_at=time.monotonic() + 3600,
        last_heartbeat=time.monotonic(),
    )


# ---------------------------------------------------------------------------
# Fix 3 regression — hijack_id propagated in pause / rollback-resume messages
# ---------------------------------------------------------------------------


def test_acquire_pause_message_contains_hijack_id() -> None:
    """Regression fix 3: pause control message sent to worker must include hijack_id for correlation."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test", "lease_s": 60})

    assert r.status_code == 200
    hijack_id = r.json()["hijack_id"]

    # The pause message sent to the worker must include hijack_id
    mock_ws.send_text.assert_awaited()
    sent_calls = decode_control_payloads([call.args[0] for call in mock_ws.send_text.await_args_list])
    pause_msgs = [m for m in sent_calls if m.get("type") == "control" and m.get("action") == "pause"]
    assert pause_msgs, "No pause control message sent to worker"
    assert pause_msgs[0].get("hijack_id") == hijack_id, "hijack_id missing from pause message"


def test_acquire_already_hijacked_does_not_send_resume() -> None:
    """Regression: compensating resume must NOT be sent when acquire fails due to
    already_hijacked.  set_hijacked is a boolean — sending resume would unpause the
    legitimate owner's session."""
    app, hub = make_app()
    mock_ws = AsyncMock()

    # Pre-inject an active session so _try_acquire_rest_hijack returns (False, already_hijacked)
    existing_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(existing_id, "owner_a"),
    )

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "owner_b", "lease_s": 60})

    # The acquire should fail with 409
    assert r.status_code == 409

    # Pause was sent (to check liveness), but no resume must follow — that would
    # unpause owner_a's active session.
    sent_calls = decode_control_payloads([call.args[0] for call in mock_ws.send_text.await_args_list])
    resume_msgs = [m for m in sent_calls if m.get("type") == "control" and m.get("action") == "resume"]
    assert not resume_msgs, f"Spurious resume sent to worker while another owner holds the lease: {resume_msgs}"


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
    hub._workers["botA"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    with TestClient(app) as client:
        r = client.get(f"/worker/botA/hijack/{hijack_id}/events?after_seq=0")

    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    # No events have been appended, so the list is empty
    assert data["events"] == []
    assert data["worker_id"] == "botA"
    assert data["hijack_id"] == hijack_id


# ---------------------------------------------------------------------------
# Round-7 regression — hijack_acquire compensating resume on cancellation
# ---------------------------------------------------------------------------


def test_acquire_sends_compensating_resume_on_error_after_pause() -> None:
    """Round-7 fix 1: if an error fires after the pause is sent but before the
    session is committed, a compensating resume must be dispatched so the worker
    does not remain stuck in the paused state indefinitely."""
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    async def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated error after pause")

    with (
        patch.object(hub, "append_event", side_effect=_raise),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test"})

    assert r.status_code == 500

    sent = decode_control_payloads([c.args[0] for c in mock_ws.send_text.await_args_list])
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
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    async def _cancel(*args: object, **kwargs: object) -> None:
        raise _asyncio.CancelledError

    with (
        patch.object(hub, "append_event", side_effect=_cancel),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        client.post("/worker/bot1/hijack/acquire", json={"owner": "test"})

    sent = decode_control_payloads([c.args[0] for c in mock_ws.send_text.await_args_list])
    resume_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert resume_msgs, "compensating resume must be sent on CancelledError after pause"


def test_acquire_compensating_resume_swallows_send_worker_failure(caplog) -> None:
    """If the compensating ``send_worker`` itself raises (worker socket already
    closed, runtime error, cancellation), the finally block must log a warning
    and not propagate — otherwise we'd both fail the original request AND
    re-raise from the cleanup path, hiding the real error.

    Exercises rest.py:231-232 (the except branch of the compensating resume).
    """
    import asyncio as _asyncio
    import logging
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    async def _append_event_raises(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated commit-time failure")

    async def _send_worker_raises(*args: object, **kwargs: object) -> bool:
        raise _asyncio.CancelledError

    caplog.set_level(logging.WARNING, logger="provide.uterm.bridge.routes.rest")
    with (
        patch.object(hub, "append_event", side_effect=_append_event_raises),
        patch.object(hub, "send_worker", side_effect=_send_worker_raises),
        TestClient(app, raise_server_exceptions=False) as client,
    ):
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test"})

    # The original failure surfaces as a 500; the compensating-resume failure
    # is logged but swallowed.
    assert r.status_code == 500
    assert any(
        "hijack_acquire_compensating_resume_failed" in rec.getMessage() for rec in caplog.records
    ), "compensating-resume failure must be logged via the rest router warning"


def test_acquire_no_compensating_resume_on_success() -> None:
    """Round-7 fix 1: a successful acquire must NOT send an extra compensating resume."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test"})

    assert r.status_code == 200

    sent = decode_control_payloads([c.args[0] for c in mock_ws.send_text.await_args_list])
    resume_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert not resume_msgs, "no resume should be sent after a successful acquire"


def test_acquire_no_resume_on_race_loss() -> None:
    """When _try_acquire_rest_hijack returns (False, already_hijacked), no resume
    must be sent.  The pre-lock pause was a no-op (worker already paused); sending
    resume would unpause the legitimate owner's session."""
    app, hub = make_app()
    mock_ws = AsyncMock()
    existing_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(existing_id, "owner_a"),
    )

    with TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "owner_b"})

    assert r.status_code == 409

    sent = decode_control_payloads([c.args[0] for c in mock_ws.send_text.await_args_list])
    resume_msgs = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert not resume_msgs, f"resume must not be sent on already_hijacked race loss: {resume_msgs}"


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
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    original_expires = hub._workers["bot1"].hijack_session.lease_expires_at  # type: ignore[union-attr]
    extended_expires = original_expires + 3600

    async def _extend_and_return(bot_id: str, timeout_ms: int = 1500) -> dict:
        # Simulate a concurrent heartbeat mutating the lease while we wait
        st = hub._workers.get(bot_id)
        if st and st.hijack_session:
            st.hijack_session.lease_expires_at = extended_expires
        return {"screen": "hello", "cols": 80, "rows": 25}

    with patch.object(hub, "wait_for_snapshot", side_effect=_extend_and_return), TestClient(app) as client:
        r = client.get(f"/worker/bot1/hijack/{hijack_id}/snapshot?wait_ms=100")

    assert r.status_code == 200
    data = r.json()
    # REST response converts monotonic to wall-clock via _mono_to_wall()
    wall_expected = extended_expires + (time.time() - time.monotonic())
    assert abs(data["lease_expires_at"] - wall_expected) < 2.0, (
        f"expected ~{wall_expected}, got {data['lease_expires_at']}"
    )


def test_snapshot_falls_back_to_original_lease_if_session_gone() -> None:
    """Round-7 fix 3: if the session is released during the snapshot wait, the
    originally-captured lease_expires_at is used (session gone guard)."""
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hijack_id = str(uuid.uuid4())
    hub._workers["bot1"] = WorkerTermState(
        worker_ws=mock_ws,
        hijack_session=_active_session(hijack_id),
    )

    original_expires = hub._workers["bot1"].hijack_session.lease_expires_at  # type: ignore[union-attr]

    async def _release_and_return(bot_id: str, timeout_ms: int = 1500) -> dict:
        # Simulate the session being released while waiting for a snapshot
        st = hub._workers.get(bot_id)
        if st:
            st.hijack_session = None
        return {"screen": "bye"}

    with patch.object(hub, "wait_for_snapshot", side_effect=_release_and_return), TestClient(app) as client:
        r = client.get(f"/worker/bot1/hijack/{hijack_id}/snapshot?wait_ms=100")

    assert r.status_code == 200
    data = r.json()
    # Falls back to the originally-captured expiry (converted to wall-clock)
    wall_expected = original_expires + (time.time() - time.monotonic())
    assert abs(data["lease_expires_at"] - wall_expected) < 2.0


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
    worker WS is pre-registered in hub._workers.
    """
    from unittest.mock import patch

    app, hub = make_app()
    str(uuid.uuid4())

    # Patch _send_worker to always succeed (simulates worker present at send time)
    # and _try_acquire_rest_hijack to grant the lock.
    async def _fake_send(bot_id: str, msg: dict) -> bool:
        return True

    async def _fake_acquire(bot_id: str, **kw):
        return True, None

    with (
        patch.object(hub, "send_worker", side_effect=_fake_send),
        patch.object(hub, "try_acquire_rest_hijack", side_effect=_fake_acquire),
        TestClient(app) as client,
    ):
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "tester"})

    assert r.status_code == 200
    assert r.json()["ok"] is True


# ---------------------------------------------------------------------------
# no_worker race: _try_acquire_rest_hijack returns "no_worker"
# ---------------------------------------------------------------------------


def test_acquire_no_worker_race_sends_exactly_one_resume() -> None:
    """Worker disconnects between _send_worker succeeding and _try_acquire_rest_hijack
    acquiring the lock.  The route must:
    - return 409
    - send exactly one resume (from the not-acquired branch), not two
      (the finally block must be suppressed via session_committed=True).

    When _try_acquire_rest_hijack returns (False, "no_worker") the _send_worker
    call in the not-acquired branch is a no-op (worker_ws is None), which is
    correct — there is nobody to resume.
    """
    from unittest.mock import patch

    app, hub = make_app()
    mock_ws = AsyncMock()
    hub._workers["bot1"] = WorkerTermState(worker_ws=mock_ws)

    async def _no_worker(worker_id: str, **kw: object) -> tuple[bool, str | None]:
        return False, "no_worker"

    with patch.object(hub, "try_acquire_rest_hijack", side_effect=_no_worker), TestClient(app) as client:
        r = client.post("/worker/bot1/hijack/acquire", json={"owner": "test"})

    assert r.status_code == 409

    sent = decode_control_payloads([c.args[0] for c in mock_ws.send_text.await_args_list])
    resumes = [m for m in sent if m.get("type") == "control" and m.get("action") == "resume"]
    assert len(resumes) == 1, f"exactly one resume expected when no_worker returned, got {len(resumes)}"
