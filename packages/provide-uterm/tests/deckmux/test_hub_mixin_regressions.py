#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Regression and mutation-killing tests for DeckMuxMixin."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from provide.uterm.deckmux._hub_mixin import DeckMuxMixin


class _FakeHub(DeckMuxMixin):
    def __init__(self) -> None:
        self._deckmux_init()
        self.broadcast = AsyncMock()


@dataclass
class _FakePrincipal:
    subject_id: str
    display_name: str = ""


class _FakeWS:
    pass


@pytest.mark.asyncio
async def test_control_grant_transfer_manager_keyed_by_worker_id() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    assert "w1" in hub._transfer_managers
    assert None not in hub._transfer_managers


def test_transfer_manager_empty_config_uses_defaults() -> None:
    hub = _FakeHub()
    tm = hub._get_transfer_manager("w1", {})

    assert tm._auto_idle_s == 30
    assert tm.queue_mode == "display"


@pytest.mark.asyncio
async def test_presence_update_broadcast_uses_worker_id_not_none() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "scroll_line": 5})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_presence_update_scroll_range_forwarded() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "scroll_range": [5, 29]})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert list(broadcast_msg.get("scroll_range", [])) == [5, 29]


@pytest.mark.asyncio
async def test_presence_update_selection_forwarded() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    selection = {"start": 10, "end": 20}
    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "selection": selection})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg.get("selection") == selection


@pytest.mark.asyncio
async def test_presence_update_pin_forwarded() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "operator")
    hub.broadcast.reset_mock()

    pin = {"line": 42}
    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "pin": pin})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg.get("pin") == pin


@pytest.mark.asyncio
async def test_non_owner_typing_does_not_reset_warning() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    tm = hub._get_transfer_manager("w1")
    tm._warning_sent = True

    await hub.deckmux_handle_message("w1", ws, {"type": "presence_update", "typing": True})

    assert tm._warning_sent is True


@pytest.mark.asyncio
async def test_queued_input_missing_keys_field_defaults_to_empty() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "queued_input"})

    hub.broadcast.assert_called_once()
    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg.get("queued_keys") == ""


@pytest.mark.asyncio
async def test_queued_input_broadcast_uses_worker_id_not_none() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "queued_input", "keys": "a"})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_queued_input_transfer_manager_keyed_by_worker_id() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")
    await hub.deckmux_handle_message("w1", ws, {"type": "queued_input", "keys": "abc"})

    assert "w1" in hub._transfer_managers
    assert None not in hub._transfer_managers


@pytest.mark.asyncio
async def test_queued_input_keys_isolated_per_user() -> None:
    hub = _FakeHub()
    ws_a = _FakeWS()
    ws_b = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws_a, "viewer")
    await hub.deckmux_on_browser_connect("w1", ws_b, "viewer")

    await hub.deckmux_handle_message("w1", ws_a, {"type": "queued_input", "keys": "abc"})
    hub.broadcast.reset_mock()
    await hub.deckmux_handle_message("w1", ws_b, {"type": "queued_input", "keys": "xyz"})

    broadcast_msg = hub.broadcast.call_args[0][1]
    assert broadcast_msg["queued_keys"] == "xyz"


@pytest.mark.asyncio
async def test_control_grant_reason_is_handover() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    _, msg = hub.broadcast.call_args[0]
    assert msg["reason"] == "handover"


@pytest.mark.asyncio
async def test_control_release_reason_is_handover() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    _, msg = hub.broadcast.call_args[0]
    assert msg["reason"] == "handover"


@pytest.mark.asyncio
async def test_control_grant_broadcast_uses_worker_id_not_none() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_control_release_broadcast_uses_worker_id_not_none() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "admin")
    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})
    hub.broadcast.reset_mock()

    await hub.deckmux_handle_message("w1", ws, {"type": "control_request"})

    worker_id_arg = hub.broadcast.call_args[0][0]
    assert worker_id_arg == "w1"
    assert worker_id_arg is not None


@pytest.mark.asyncio
async def test_connect_sync_config_auto_transfer_idle_s_is_30() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "viewer")

    assert result is not None
    config = result.get("config")
    assert isinstance(config, dict)
    assert config["auto_transfer_idle_s"] == 30


@pytest.mark.asyncio
async def test_connect_sync_config_keystroke_queue_is_display() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "viewer")

    assert result is not None
    config = result.get("config")
    assert isinstance(config, dict)
    assert config["keystroke_queue"] == "display"


@pytest.mark.asyncio
async def test_connect_prunes_at_30_not_31_seconds() -> None:
    import time

    hub = _FakeHub()
    ws_stale = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws_stale, "viewer")
    store = hub._get_presence_store("w1")
    stale_id = ws_stale._deckmux_anon_id
    store._users[stale_id].last_activity_at = time.time() - 30.5

    ws_new = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws_new, "operator")

    assert result is not None
    user_ids = [u["user_id"] for u in result["users"]]
    assert stale_id not in user_ids


@pytest.mark.asyncio
async def test_principal_truthy_without_subject_id_uses_generated_name() -> None:
    class _TruthyNoSubject:
        pass

    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "viewer", principal=_TruthyNoSubject())

    assert result is not None
    user = result["users"][0]
    assert user["name"] != ""
    assert user["user_id"] == ws._deckmux_anon_id


@pytest.mark.asyncio
async def test_principal_no_display_name_attr_falls_back_to_subject_id() -> None:
    class _PrincipalNoDisplayName:
        def __init__(self, subject_id: str) -> None:
            self.subject_id = subject_id

    hub = _FakeHub()
    ws = _FakeWS()
    result = await hub.deckmux_on_browser_connect("w1", ws, "admin", principal=_PrincipalNoDisplayName("svc-abc"))

    assert result is not None
    user = result["users"][0]
    assert user["name"] == "svc-abc"


@pytest.mark.asyncio
async def test_colors_avoid_collision_with_taken_colors() -> None:
    from provide.uterm.deckmux._names import _COLORS, _hash_int

    ids_by_idx: dict[int, list[str]] = {}
    for i in range(500):
        sid = f"col-{i}"
        h = _hash_int(sid)
        idx = h % len(_COLORS)
        ids_by_idx.setdefault(idx, []).append(sid)

    collision_pair = next((ids[:2] for ids in ids_by_idx.values() if len(ids) >= 2), None)
    assert collision_pair is not None, "No collision found among 500 IDs"
    id1, id2 = collision_pair
    hub = _FakeHub()
    ws1, ws2 = _FakeWS(), _FakeWS()

    await hub.deckmux_on_browser_connect("w1", ws1, "viewer", principal=_FakePrincipal(subject_id=id1))
    result = await hub.deckmux_on_browser_connect("w1", ws2, "viewer", principal=_FakePrincipal(subject_id=id2))

    assert result is not None
    colors = [u["color"] for u in result["users"]]
    assert colors[0] != colors[1]


@pytest.mark.asyncio
async def test_second_connect_broadcasts_sync_to_existing_users() -> None:
    hub = _FakeHub()
    ws1, ws2 = _FakeWS(), _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws1, "viewer")
    hub.broadcast.assert_not_called()

    await hub.deckmux_on_browser_connect("w1", ws2, "operator")

    hub.broadcast.assert_called_once()
    worker_id_arg, msg = hub.broadcast.call_args[0]
    assert worker_id_arg == "w1"
    assert msg["type"] == "presence_sync"
    assert len(msg["users"]) == 2


@pytest.mark.asyncio
async def test_first_connect_does_not_broadcast() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    await hub.deckmux_on_browser_connect("w1", ws, "viewer")

    hub.broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_disconnect_uses_subject_id() -> None:
    hub = _FakeHub()
    ws = _FakeWS()
    principal = _FakePrincipal(subject_id="alice")
    await hub.deckmux_on_browser_connect("w1", ws, "operator", principal=principal)
    hub.broadcast.reset_mock()

    await hub.deckmux_on_browser_disconnect("w1", ws, principal=principal)

    hub.broadcast.assert_called_once()
    msg = hub.broadcast.call_args[0][1]
    assert msg["type"] == "presence_leave"
    assert msg["user_id"] == "alice"


@pytest.mark.asyncio
async def test_authenticated_disconnect_wrong_ws_id_ghost_absence() -> None:
    hub = _FakeHub()
    ws_connect = _FakeWS()
    ws_disconnect = _FakeWS()
    principal = _FakePrincipal(subject_id="bob")
    await hub.deckmux_on_browser_connect("w1", ws_connect, "operator", principal=principal)
    hub.broadcast.reset_mock()

    await hub.deckmux_on_browser_disconnect("w1", ws_disconnect)

    hub.broadcast.assert_not_called()
