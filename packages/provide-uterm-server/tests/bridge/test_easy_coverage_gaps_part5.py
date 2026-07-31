#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted unit tests covering single-line gaps surfaced by the coverage
report (the "easy wins" set).

Each test docstring names the file:line it covers so future report-vs-test
correlation is obvious.
"""

from __future__ import annotations

import logging

import pytest

# ---------------------------------------------------------------------------
# bridge/hub/core.py:180 — set_worker_hello_mode rejects invalid input mode
# ---------------------------------------------------------------------------


async def test_set_worker_hello_mode_rejects_invalid_mode() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    hub = TermHub()
    with pytest.raises(ValueError, match="invalid input mode"):
        await hub.set_worker_hello_mode("nobody", "bogus-mode")


# ---------------------------------------------------------------------------
# bridge/hub/connections.py:205 — legacy protocol_version warning log
# ---------------------------------------------------------------------------


async def test_worker_hello_logs_warning_for_legacy_protocol(caplog: pytest.LogCaptureFixture) -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.models import WorkerTermState

    hub = TermHub()
    hub.registry._workers["w1"] = WorkerTermState(worker_ws=AsyncMock())
    # Phase 6 of refactor #16: log lives on the new ConnectionManager module.
    caplog.set_level(logging.WARNING, logger="provide.uterm.server.bridge.hub.connection")
    await hub.set_worker_hello("w1", "open", protocol_version=0)
    assert any("worker_hello_legacy_protocol" in r.getMessage() for r in caplog.records), (
        "set_worker_hello with protocol_version<1 must log worker_hello_legacy_protocol warning"
    )


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:16 — CommandSplitter.split("") -> []
# ---------------------------------------------------------------------------


def test_fanout_controller_works_without_approval_store_on_hub() -> None:
    """bridge/fanout/_controller.py:46->exit — hub without _approval_store doesn't crash init."""
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.fanout._controller import FanOutController

    hub = MagicMock(spec=[])  # no attributes -> _approval_store getattr returns None
    ctrl = FanOutController(hub=hub, fanout_policy_gate=MagicMock())
    # The controller still initializes; the missing-store branch is a no-op.
    assert ctrl._pending_approvals == {}


async def test_browser_handlers_multi_part_split_routes_each() -> None:
    """browser_handlers.py:289->291 — multi-part command takes the else branch."""
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.server.bridge.routes.browser_handlers import _handle_input

    class AllowGate:
        async def intercept_input(self, _data: str, _ctx: PolicyContext) -> PolicyDecision:
            return PolicyDecision(action="allow")

    hub = TermHub(policy_gate=AllowGate())
    ws = AsyncMock()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    await hub.register_browser("w1", ws, "admin")
    await hub.try_acquire_ws_hijack("w1", ws)

    # ; separator yields two parts -> loop runs twice
    await _handle_input(hub, ws, "w1", {"type": "input", "data": "echo a; echo b\n"})
    worker_ws.send_text.assert_called()


async def test_resolve_approval_paused_browser_resumed_without_hold_buffer() -> None:
    """approvalflow.py:83->113 — paused browser unpaused, allow decision but no buffered input."""
    import time as _time
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
    from provide.uterm.server.bridge.hub.ext import PolicyDecision

    hub = TermHub()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    browser_ws = AsyncMock()
    await hub.register_browser("w1", browser_ws, "admin")
    hub._paused_browsers.add(browser_ws)  # paused, but no entry in _hold_buffers
    hub.approval_store.add(
        ApprovalRequest(
            id="r-noh",
            worker_id="w1",
            submitter_id="a",
            command="x",
            status=ApprovalStatus.PENDING,
            created_at=_time.time(),
            expires_at=_time.time() + 60,
            is_fanout=False,
        ),
    )
    await hub.resolve_approval("w1", "r-noh", PolicyDecision(action="allow"), "x")
    assert browser_ws not in hub._paused_browsers


async def test_handle_input_post_split_send_failure_notifies_browser() -> None:
    """browser_handlers.py:325 — custom (non-NoOp) gate routes through the
    splitter/per-part path; the trailing send_worker failure must still
    emit the Worker-connection-lost frame to the browser."""
    from unittest.mock import AsyncMock, patch

    from provide.uterm.server.bridge.hub import PolicyContext, PolicyDecision, TermHub
    from provide.uterm.server.bridge.routes.browser_handlers import _handle_input

    class AllowGate:
        async def intercept_input(self, _data: str, _ctx: PolicyContext) -> PolicyDecision:
            return PolicyDecision(action="allow")

    hub = TermHub(policy_gate=AllowGate())
    ws = AsyncMock()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    await hub.register_browser("w1", ws, "admin")
    await hub.try_acquire_ws_hijack("w1", ws)

    with patch.object(hub, "send_worker", AsyncMock(return_value=False)):
        # Complete chunk (newline) so the buffer flushes and we reach the
        # post-split send_worker call at line 322.
        await _handle_input(hub, ws, "w1", {"type": "input", "data": "echo hi\n"})

    payload = ws.send_text.await_args.args[0]
    assert "Worker connection lost" in payload


# ---------------------------------------------------------------------------
# server/runtime.py:429 — `outcome == "cancelled"` break path.
# ---------------------------------------------------------------------------


async def test_classify_run_error_classifies_known_categories() -> None:
    """Round-trip _classify_run_error to be sure the cancelled-mapping holds."""
    import asyncio as _asyncio

    from provide.uterm.server.runtime import _classify_run_error

    # Cancellation is the only branch that breaks the runtime's reconnect loop.
    assert _classify_run_error(_asyncio.CancelledError()) == "cancelled"


# ---------------------------------------------------------------------------
# server/dev_idp.py:94->97 — setup_dev_idp preserves an existing worker
# bearer token (False branch of `if not auth.worker_bearer_token`).
# ---------------------------------------------------------------------------


def test_setup_dev_idp_preserves_caller_supplied_bearer(monkeypatch, tmp_path) -> None:
    from provide.uterm.server.dev_idp import setup_dev_idp
    from provide.uterm.server.models import AuthConfig

    monkeypatch.setenv("UTERM_DEV_TOKEN_PATH", str(tmp_path / "tok"))
    auth = AuthConfig(mode="dev_token", worker_bearer_token="caller-supplied-bearer-token-x")
    setup_dev_idp(auth)
    assert auth.worker_bearer_token == "caller-supplied-bearer-token-x"


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:85->87, 94->97 — CommandSplitter skips empty segments
# both mid-command (`;;`) and at the end (`cmd;`).
# ---------------------------------------------------------------------------


def test_command_splitter_skips_empty_segments() -> None:
    from provide.uterm.server.bridge.hub.semantics import CommandSplitter

    splitter = CommandSplitter()
    # Empty mid-segment from `;;`
    assert splitter.split("a;;b") == ["a", "b"]
    # Empty trailing segment from `a;`
    assert splitter.split("a;") == ["a"]
    # Whitespace-only mid-segment is also empty after strip
    assert splitter.split("a;   ;b") == ["a", "b"]


# ---------------------------------------------------------------------------
# bridge/fanout/_controller.py — approval release returns an explicit error
# when the full principal no longer passes current authorization.
# ---------------------------------------------------------------------------


async def test_fanout_release_approved_command_returns_error_for_unauthorized_principal() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from provide.uterm.server.bridge.fanout._controller import FanOutController
    from provide.uterm.server.bridge.identity import Principal

    hub = MagicMock()
    ctrl = FanOutController(
        hub=hub,
        fanout_policy_gate=MagicMock(),
        is_global_admin=AsyncMock(return_value=False),
        resolve_session=AsyncMock(return_value=object()),
        can_read_session=AsyncMock(return_value=True),
    )
    ctrl._pending_approvals["req-y"] = {  # type: ignore[attr-defined]
        "group_id": "g-gone",
        "command": "ls",
        "principal": Principal(subject_id="user", roles=frozenset({"admin"})),
        "quiesce_ms": None,
        "max_response_ms": None,
    }
    result = await ctrl.release_approved_command("req-y")

    assert result is not None
    assert result.error == "global admin role required"
    hub.send_worker.assert_not_called()
    hub.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# bridge/routes/browser_handlers.py:289->291 — buffered prefix branch in
# _handle_input when the chunk completes a buffered command.
# ---------------------------------------------------------------------------


def test_gateway_read_token_returns_none_for_malformed_json_dict(tmp_path) -> None:
    """gateway/_gateway.py:58-59 — JSON parses but result isn't a dict-with-token."""
    from provide.uterm.gateway._gateway import _read_token

    # Valid JSON, but not a dict -> None
    p1 = tmp_path / "tok1.json"
    p1.write_text('["just", "a", "list"]')
    assert _read_token(p1) is None

    # Dict without "token" -> None
    p2 = tmp_path / "tok2.json"
    p2.write_text('{"other": "field"}')
    assert _read_token(p2) is None

    # Dict with empty token -> None
    p3 = tmp_path / "tok3.json"
    p3.write_text('{"token": ""}')
    assert _read_token(p3) is None


def test_gateway_write_token_swallows_oserror(tmp_path, monkeypatch) -> None:
    """gateway/_gateway.py:155-156 — _write_token's OSError suppression."""
    # Force os.chmod to raise; _write_token writes the file then chmods,
    # suppressing any OSError from chmod (e.g. on a read-only FS).
    import os

    from provide.uterm.gateway._gateway import _write_token

    p = tmp_path / "tok.json"
    original_chmod = os.chmod

    def _raising_chmod(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "chmod", _raising_chmod)
    try:
        _write_token(p, "tok-abc", 42)
    finally:
        monkeypatch.setattr(os, "chmod", original_chmod)
    # The file content was written before the chmod failure.
    assert "tok-abc" in p.read_text()


def test_gateway_delete_token_swallows_missing_file(tmp_path) -> None:
    """gateway/_gateway.py:_delete_token tolerates a missing file."""
    from provide.uterm.gateway._gateway import _delete_token

    _delete_token(tmp_path / "does-not-exist")  # must not raise


async def test_gateway_handle_control_session_token_persists_to_disk(tmp_path) -> None:
    """gateway/_gateway.py:147-157 — session_token frame writes token + player_id to disk."""
    from provide.uterm.gateway._gateway import _handle_ws_control_frame

    holder: list[dict | None] = [None]
    written: list[bytes] = []

    async def _write_fn(b: bytes) -> None:
        written.append(b)

    token_file = tmp_path / "tok.json"
    ok = await _handle_ws_control_frame(
        {"type": "session_token", "token": "tk1", "player_id": 7},
        holder,
        _write_fn,
        token_file=token_file,
    )
    assert ok is True
    assert holder[0] == {"token": "tk1", "player_id": 7}
    assert "tk1" in token_file.read_text()


async def test_gateway_handle_control_resume_ok_emits_session_resumed_message(tmp_path) -> None:
    """gateway/_gateway.py:158-160 — resume_ok writes [Session resumed] to the client."""
    from provide.uterm.gateway._gateway import _handle_ws_control_frame

    written: list[bytes] = []

    async def _write_fn(b: bytes) -> None:
        written.append(b)

    ok = await _handle_ws_control_frame({"type": "resume_ok"}, [None], _write_fn, token_file=None)
    assert ok is True
    assert any(b"[Session resumed]" in b for b in written)


async def test_gateway_handle_control_resume_failed_deletes_token_file(tmp_path) -> None:
    """gateway/_gateway.py:161-165 — resume_failed clears the token holder and deletes the file."""
    from provide.uterm.gateway._gateway import _handle_ws_control_frame

    async def _write_fn(_b: bytes) -> None:  # pragma: no cover — resume_failed doesn't write
        pass

    token_file = tmp_path / "tok.json"
    token_file.write_text('{"token":"stale"}')
    holder: list[dict | None] = [{"token": "stale"}]
    ok = await _handle_ws_control_frame({"type": "resume_failed"}, holder, _write_fn, token_file=token_file)
    assert ok is True
    assert holder[0] is None
    assert not token_file.exists()


async def test_gateway_handle_control_unknown_type_returns_false() -> None:
    """gateway/_gateway.py:166 — unknown control type returns False."""
    from provide.uterm.gateway._gateway import _handle_ws_control_frame

    async def _write_fn(_b: bytes) -> None:  # pragma: no cover — unknown type doesn't write
        pass

    assert await _handle_ws_control_frame({"type": "unknown"}, [None], _write_fn) is False

    # Non-dict data.get -> AttributeError -> False.
    class _NotADict:
        def get(self, _key):
            raise AttributeError

    assert await _handle_ws_control_frame(_NotADict(), [None], _write_fn) is False


async def test_handle_input_completing_buffered_command_passes_full_string() -> None:
    """When a prior partial chunk landed in _input_buffers, a completing chunk
    should be joined with the prefix before the worker sees it."""
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.routes.browser_handlers import _handle_input

    hub = TermHub()
    ws = AsyncMock()
    worker_ws = AsyncMock()
    await hub.register_worker("w1", worker_ws)
    await hub.register_browser("w1", ws, "admin")
    await hub.try_acquire_ws_hijack("w1", ws)

    # Send a partial first chunk; no newline so it's buffered.
    await _handle_input(hub, ws, "w1", {"type": "input", "data": "ec"})
    # Now the completing chunk; the policy gate sees the joined "echo hi\n".
    await _handle_input(hub, ws, "w1", {"type": "input", "data": "ho hi\n"})
    # Verify the worker received "echo hi" data via the second call.
    sent_payloads = [c.args[0] for c in worker_ws.send_text.await_args_list]
    joined = "".join(sent_payloads)
    assert "echo hi" in joined
