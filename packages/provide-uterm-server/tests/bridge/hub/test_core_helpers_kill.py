#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``hub.core_helpers``'s frame encoders and ``register_ws_routes``.

``_encode_browser_frame``/``_encode_worker_frame`` each guard two fields with
an ``or ""`` default: ``msg.get("type")`` (used only for an ``== "term"`` /
``== "input"`` comparison) and ``msg.get("data")`` (fed straight into
``encode_terminal_data``). Every existing frame in every other test carries
both fields, so the defaults are never exercised there. The two defaults are
not equally sensitive:

*``type``'s default is genuinely dead.* It is compared only against the
literal ``"term"``/``"input"``; swapping ``""`` for any other non-matching
sentinel (e.g. ``"XXXX"``) can never make that comparison true when it
wasn't, and can never make it false when it was (a present, truthy
``"term"``/``"input"`` short-circuits the ``or`` before the default is even
consulted). Confirmed empirically against every combination of a present/
absent/empty ``type`` key.

*``data``'s default is not.* It is the wire payload itself. With the field
missing, the real default makes ``encode_terminal_data("")`` -- an empty
terminal chunk -- while a non-``""`` sentinel would ship that sentinel text
as if it were real terminal output.

``register_ws_routes`` wires exactly one piece of hub state before
registering its two websocket routes: ``hub._on_browser_message``. Nothing
about the websocket routes themselves is reachable without a live socket, so
that one assignment is the only thing this file needs to pin.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter

from provide.uterm.control_channel import encode_terminal_data
from provide.uterm.server.bridge.hub.core_helpers import _encode_browser_frame, _encode_worker_frame
from provide.uterm.server.bridge.routes.browser_handlers import handle_browser_message
from provide.uterm.server.bridge.routes.websockets_impl import register_ws_routes


def test_browser_frame_with_no_data_field_ships_an_empty_chunk_not_a_sentinel() -> None:
    """A ``term`` frame missing ``data`` must encode ``""``, not a non-empty default."""
    assert _encode_browser_frame({"type": "term"}) == encode_terminal_data("")


def test_worker_frame_with_no_data_field_ships_an_empty_chunk_not_a_sentinel() -> None:
    """Mirror of the browser case for ``_encode_worker_frame``'s ``input`` frames."""
    assert _encode_worker_frame({"type": "input"}) == encode_terminal_data("")


def test_register_ws_routes_wires_the_browser_message_handler_onto_the_hub() -> None:
    """``register_ws_routes`` must point ``hub._on_browser_message`` at the real handler.

    Nothing else it does is observable without opening an actual websocket
    connection, so this one assignment is the whole contract.
    """

    class _StubHub:
        _on_browser_message: Any = None

    hub = _StubHub()
    router = APIRouter()

    register_ws_routes(cast("Any", hub), router)

    assert hub._on_browser_message is handle_browser_message
