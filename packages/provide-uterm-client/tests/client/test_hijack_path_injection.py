#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""HijackClient path-injection guard.

Caller/LLM-supplied ``worker_id`` / ``hijack_id`` / ``session_id`` are
interpolated into request paths. An id like ``../../api/keys`` would let httpx
resolve the URL to a *different* server route, escaping the per-method authz
model. ``_safe_id`` rejects anything that is not a single safe path segment,
before any HTTP request is issued.
"""

from __future__ import annotations

import pytest

from provide.uterm.client.hijack import HijackClient, _safe_id

GOOD_IDS = ["worker-1", "wh1", "s1", "session.1", "a_b-C.3", "UUID-1234-5678", "x"]
BAD_IDS = ["../../api/keys", "..", ".", "a/b", "w%2fx", "", "wo rker", "a\\b", "with/slash", "tab\tid"]


@pytest.mark.parametrize("value", GOOD_IDS)
def test_safe_id_accepts_single_safe_segment(value: str) -> None:
    assert _safe_id(value) == value


@pytest.mark.parametrize("value", BAD_IDS)
def test_safe_id_rejects_traversal_and_separators(value: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        _safe_id(value, "worker_id")


def _client() -> HijackClient:
    # No transport needed: validation raises before any request is built.
    return HijackClient("http://test")


def test_path_builders_validate_ids() -> None:
    c = _client()
    # Valid ids build the expected single-segment paths.
    assert c._wp("worker-1") == f"{c._entity_prefix}/worker-1"
    assert c._hp("worker-1", "hj-2") == f"{c._entity_prefix}/worker-1/hijack/hj-2"
    assert c._sp("s1") == "/api/sessions/s1"
    # Traversal / separators are rejected at each interpolation point.
    with pytest.raises(ValueError, match="worker_id"):
        c._wp("../../api/keys")
    with pytest.raises(ValueError, match="worker_id"):
        c._hp("a/b", "hj")
    with pytest.raises(ValueError, match="hijack_id"):
        c._hp("worker-1", "../x")
    with pytest.raises(ValueError, match="session_id"):
        c._sp("../keys")


async def test_public_methods_reject_injected_ids() -> None:
    c = _client()
    # A worker-path method (acquire → _wp), a hijack-path method (snapshot → _hp),
    # and a session-path method (get_session → _sp) all fail closed before HTTP.
    with pytest.raises(ValueError, match="worker_id"):
        await c.acquire("../../api/keys")
    with pytest.raises(ValueError, match="hijack_id"):
        await c.snapshot("worker-1", "../../api/keys")
    with pytest.raises(ValueError, match="session_id"):
        await c.get_session("../../api/keys")
