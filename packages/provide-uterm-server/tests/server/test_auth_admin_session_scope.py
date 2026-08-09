#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A JWT may confine its ``admin`` role to a single session."""

from __future__ import annotations

import pytest

from provide.uterm.server.auth import _admin_session_scope_from_claims
from provide.uterm.server.authorization import LocalAuthorizationProvider
from provide.uterm.server.bridge.identity import Principal


def _session(session_id: str, owner: str | None = None):
    class _Definition:
        pass

    definition = _Definition()
    definition.session_id = session_id
    definition.owner = owner
    definition.visibility = "private"
    return definition


def test_absent_claim_leaves_existing_tokens_global() -> None:
    assert _admin_session_scope_from_claims({"sub": "someone"}) is None


@pytest.mark.parametrize(
    "value",
    ["", "   ", "has space", "../escape", "a" * 129, "semi;colon"],
)
def test_malformed_scopes_are_refused(value: str) -> None:
    assert _admin_session_scope_from_claims({"admin_session_scope": value}) is None


def test_a_well_formed_scope_is_accepted() -> None:
    assert _admin_session_scope_from_claims({"admin_session_scope": "pam-suokki-capture-42"}) == (
        "pam-suokki-capture-42"
    )


@pytest.mark.asyncio
async def test_a_scoped_admin_is_not_a_global_admin() -> None:
    provider = LocalAuthorizationProvider()
    scoped = Principal(
        subject_id="blackbetty:7",
        roles=frozenset({"admin"}),
        scopes=frozenset({"*"}),
        admin_session_scope="session-a",
    )

    assert await provider.is_admin(scoped) is False
    assert provider._is_admin_for_session(scoped, _session("session-a")) is True
    assert provider._is_admin_for_session(scoped, _session("session-b")) is False


@pytest.mark.asyncio
async def test_an_unscoped_admin_keeps_global_reach() -> None:
    provider = LocalAuthorizationProvider()
    globally = Principal(subject_id="ops", roles=frozenset({"admin"}), scopes=frozenset({"*"}))

    assert await provider.is_admin(globally) is True
    assert provider._is_admin_for_session(globally, _session("anything")) is True
