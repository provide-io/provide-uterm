#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for the ``rest_gui`` module-level helpers.

Kill-suite only — behavioural coverage lives in ``test_rest_gui.py``. Every test
here exists to distinguish a specific surviving mutant, and each says which.

Scope note worth keeping: mutmut skips decorated functions, and all six ``/gui/``
handlers are ``@router.*``-decorated, so a 321-line file yields 91 mutants — the
module-level helpers below and nothing else. Adding this file to the perimeter
enforces the helpers (including ``_block_private``, which decides whether a
private/loopback console may be dialled at all); it does NOT enforce the handler
bodies. Un-decorating them would expose those, and would land several hundred
mutants at once — see ``9bc4dd0c`` in CLAUDE.md for how that goes.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.routes.rest_gui import (
    _block_private,
    _mono_to_wall,
    _require_graphical_session,
)

WID = "gui-worker"
HID = "00000000-0000-0000-0000-000000000000"


def _request(*, config: Any = "OMIT", principal: Any = None) -> Any:
    """A request whose app.state carries (or deliberately lacks) uterm_config."""
    state = SimpleNamespace()
    if config != "OMIT":
        state.uterm_config = config
    return SimpleNamespace(app=SimpleNamespace(state=state), state=SimpleNamespace(uterm_principal=principal))


# ---------------------------------------------------------------------------
# _block_private
# ---------------------------------------------------------------------------


class TestBlockPrivate:
    def test_a_configured_true_flag_is_read_all_the_way_down(self) -> None:
        """Kills every mutant that severs the config → security → flag chain.

        mutmut_1/2 (config forced None), 7/8 ("uterm_config" mangled), 9/10
        (security forced None), 15/16 ("security" mangled), 17 (bool(None)),
        18 (getattr on None), 24/25 ("block_private_connector_targets"
        mangled). Each returns False where the real lookup returns True, so one
        positive read kills all twelve. Asserting only the False default — which
        is what the handler tests do — distinguishes none of them.
        """
        req = _request(config=SimpleNamespace(security=SimpleNamespace(block_private_connector_targets=True)))
        assert _block_private(req) is True

    def test_no_config_at_all_does_not_block(self) -> None:
        """The default must stay False: a deployment that configures nothing
        still reaches its own internal consoles. Guards the getattr defaults
        against being flipped to True."""
        assert _block_private(_request()) is False

    def test_config_without_a_security_section_does_not_block(self) -> None:
        assert _block_private(_request(config=SimpleNamespace())) is False

    def test_security_without_the_flag_does_not_block(self) -> None:
        assert _block_private(_request(config=SimpleNamespace(security=SimpleNamespace()))) is False

    def test_a_truthy_non_bool_flag_is_coerced(self) -> None:
        """``bool()`` is load-bearing: the return type is annotated ``bool`` and
        a TOML/env-derived 1 must not leak through as an int."""
        req = _request(config=SimpleNamespace(security=SimpleNamespace(block_private_connector_targets=1)))
        result = _block_private(req)
        assert result is True


# ---------------------------------------------------------------------------
# _mono_to_wall
# ---------------------------------------------------------------------------


def test_mono_to_wall_subtracts_the_monotonic_reading() -> None:
    """Kills mutmut_2, which ADDS ``time.monotonic()`` instead of subtracting.

    Converting "now" on the monotonic clock must land on "now" on the wall
    clock. The mutant lands a full monotonic reading into the future — on a
    machine up for an hour that is an hour of drift in a lease_expires_at the
    client is told to trust.
    """
    assert abs(_mono_to_wall(time.monotonic()) - time.time()) < 1.0


# ---------------------------------------------------------------------------
# _require_graphical_session
# ---------------------------------------------------------------------------


class _ArgCheckingHub:
    """A hub that answers only for the exact (worker_id, hijack_id) pair.

    ``AsyncMock(return_value=...)`` answers regardless of arguments, which is
    why mutmut_2/3 (an argument replaced with None) and mutmut_4/5 (an argument
    dropped) survived every existing test.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self.registry = SimpleNamespace(get=lambda _wid: None)

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> Any:
        if worker_id != WID or hijack_id != HID:
            return None
        return self._session


async def test_the_lease_is_looked_up_by_both_worker_and_hijack_id() -> None:
    """Kills mutmut_2/3 (an id replaced by None) and mutmut_4/5 (an id dropped).

    Dropping an argument raises TypeError against a real two-argument signature;
    passing None finds no lease and 404s. Either way the mutant cannot return
    the session this asserts on.
    """
    session = SimpleNamespace(lease_expires_at=time.monotonic() + 60, acquired_by=None)
    hub = _ArgCheckingHub(session)
    req = _request(principal=SimpleNamespace(subject_id="alice"))

    result = await _require_graphical_session(hub, req, WID, HID)

    # No graphical session is attached, so this is the 404 that proves the lease
    # WAS found — a mutant that mislooks-up the lease returns the *other* 404.
    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    import json

    assert json.loads(result.body)["error"] == "No graphical session attached."


@pytest.mark.parametrize("requester", ["mallory", "bob"])
async def test_a_lease_owned_by_someone_else_is_refused_with_its_exact_body(requester: str) -> None:
    """Kills mutmut_22 (body → None), 26/27 (the "error" key mangled) and
    28/29 (the message mangled).

    The status alone distinguishes none of them: every one of those mutants
    still returns 403. This is the refusal an operator reads in a log when an
    agent reaches for a screen it does not hold, so the wording is contract.
    """
    session = SimpleNamespace(lease_expires_at=time.monotonic() + 60, acquired_by="alice")
    hub = _ArgCheckingHub(session)
    req = _request(principal=SimpleNamespace(subject_id=requester))

    result = await _require_graphical_session(hub, req, WID, HID)

    assert isinstance(result, JSONResponse)
    assert result.status_code == 403
    import json

    assert json.loads(result.body) == {"error": "hijack lease not owned by caller"}
