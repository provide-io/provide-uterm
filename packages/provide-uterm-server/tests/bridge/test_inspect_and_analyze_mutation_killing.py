#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers._handle_analyze_req`` and
``browser_handlers._handle_http_inspect_control``.

Kill-suite only -- both functions' 20 mutants (14 for
``_handle_http_inspect_control``, 6 for ``_handle_analyze_req``) come back
``no tests`` from mutmut, which means nothing in the mutation selection ever
calls the real bodies:

- ``test_handle_browser_message_mutation_killing.py`` monkeypatches
  ``browser_handlers._handle_analyze_req`` and
  ``browser_handlers._handle_http_inspect_control`` themselves with strict
  recorder stubs, precisely so ``handle_browser_message``'s own dispatch
  arguments can be tested in isolation from what these two functions do --
  which means that suite never executes either function's real body.
- ``test_browser_handlers_coverage.py`` and ``test_browser_handlers_mutations.py``
  do not send an ``analyze_req`` or any of the ``_HTTP_INSPECT_CONTROL_TYPES``
  messages at all (per the sibling suite's own docstring above), so no other
  existing test reaches these bodies either.

A ``no tests`` mutant can never be excused via the equivalents allowlist, so
every one of the 20 must be killed here by a test that calls the real
function directly.

Everything below drives ``_handle_analyze_req``/``_handle_http_inspect_control``
against a strict fake hub whose ``touch_if_owner``/``capture_browser_ownership``/
``send_owned_worker``/``request_analysis`` methods mirror the real
``TermHub`` service signatures exactly (see
``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/lease.py``
and ``.../hub/presence.py``) and assert on every argument they receive --
including arity, so an argument silently dropped (which shifts positional
binding, or removes a required parameter) raises ``TypeError`` instead of
running unnoticed. Each test covers both the owner and non-owner path.

Documented equivalents: none. All 20 mutants (enumerated in
``scratchpad/nt__handle_http_inspect_control.txt`` and
``scratchpad/nt__handle_analyze_req.txt``) are killed below -- confirmed by
executing each mutant's extracted body against these exact fakes in a
throwaway driver script before writing this file. Every single one is caught
by the "owner" test alone, either via a failed strict-equality assertion
(a swapped ``None`` argument, a dropped keyword defaulting to ``None``, or a
branch flip that skips/adds a call) or via a ``TypeError`` raised at the call
site itself when an argument is dropped entirely and arity no longer matches
the real collaborator signature.
"""

from __future__ import annotations

from typing import Any

from provide.uterm.server.bridge.routes.browser_handlers import (
    _handle_analyze_req,
    _handle_http_inspect_control,
)

WID = "inspect-analyze-worker"
GENERATION = 7
LEASE_EXPIRES_AT = 42.0
MSG_B = {"type": "http_intercept_toggle", "enabled": True}


class _WS:
    """A browser socket. Neither function under test sends it anything."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        assert isinstance(text, str), f"send_text got {text!r}"
        self.sent.append(text)


# ---------------------------------------------------------------------------
# _handle_analyze_req
# ---------------------------------------------------------------------------


class _AnalyzeHub:
    """A hub whose collaborators assert on every argument they receive."""

    def __init__(self, ws: _WS, *, lease_expires_at: float | None) -> None:
        self.ws = ws
        self._lease_expires_at = lease_expires_at
        self.touch_calls: list[tuple[str, Any]] = []
        self.analysis_calls: list[str] = []

    async def touch_if_owner(self, worker_id: str, ws: Any) -> float | None:
        assert worker_id == WID, f"touch_if_owner got worker_id={worker_id!r}"
        assert ws is self.ws, f"touch_if_owner got ws={ws!r}"
        self.touch_calls.append((worker_id, ws))
        return self._lease_expires_at

    async def request_analysis(self, worker_id: str) -> None:
        assert worker_id == WID, f"request_analysis got worker_id={worker_id!r}"
        self.analysis_calls.append(worker_id)


async def test_analyze_req_owner_requests_analysis_with_the_exact_worker_id() -> None:
    """Kills every one of ``_handle_analyze_req``'s 6 mutants (mutmut_1-6):
    mutmut_1/2 swap ``worker_id``/``ws`` for ``None`` in the
    ``touch_if_owner`` call -- caught by the fake's argument asserts;
    mutmut_3/4 drop one of those two arguments entirely, shifting arity to 1
    positional argument against a signature that requires 2 -- caught by the
    resulting ``TypeError`` before the fake body even runs; mutmut_5 flips
    ``is not None`` to ``is None``, which for this owner scenario (a real
    lease value, not ``None``) skips ``request_analysis`` entirely -- caught
    because it must have been called; mutmut_6 replaces the
    ``request_analysis`` argument with ``None`` -- caught by that call's own
    argument assert."""
    ws = _WS()
    hub = _AnalyzeHub(ws, lease_expires_at=LEASE_EXPIRES_AT)
    await _handle_analyze_req(hub, ws, WID)
    assert hub.touch_calls == [(WID, ws)]
    assert hub.analysis_calls == [WID]


async def test_analyze_req_non_owner_requests_nothing() -> None:
    """A non-owner (``touch_if_owner`` returns ``None``) triggers no
    analysis request. Also kills mutmut_5 from the other direction: its
    flipped ``is None`` check would wrongly call ``request_analysis`` here,
    where the real function must not."""
    ws = _WS()
    hub = _AnalyzeHub(ws, lease_expires_at=None)
    await _handle_analyze_req(hub, ws, WID)
    assert hub.touch_calls == [(WID, ws)]
    assert hub.analysis_calls == []


# ---------------------------------------------------------------------------
# _handle_http_inspect_control
# ---------------------------------------------------------------------------


class _InspectHub:
    """A hub whose collaborators assert on every argument they receive."""

    def __init__(self, ws: _WS, *, generation: int | None) -> None:
        self.ws = ws
        self._generation = generation
        self.capture_calls: list[tuple[str, Any]] = []
        self.send_calls: list[tuple[str, dict[str, Any], Any, int | None]] = []

    async def capture_browser_ownership(self, worker_id: str, ws: Any) -> int | None:
        assert worker_id == WID, f"capture_browser_ownership got worker_id={worker_id!r}"
        assert ws is self.ws, f"capture_browser_ownership got ws={ws!r}"
        self.capture_calls.append((worker_id, ws))
        return self._generation

    async def send_owned_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        browser_ws: Any = None,
        rest_hijack_id: str | None = None,
        ownership_generation: int | None = None,
        source: Any = None,
    ) -> tuple[bool, str | None]:
        assert worker_id == WID, f"send_owned_worker got worker_id={worker_id!r}"
        assert msg is MSG_B, f"send_owned_worker got msg={msg!r}"
        assert browser_ws is self.ws, f"send_owned_worker got browser_ws={browser_ws!r}"
        assert rest_hijack_id is None, f"send_owned_worker got rest_hijack_id={rest_hijack_id!r}"
        assert ownership_generation is not None and ownership_generation == self._generation, (
            f"send_owned_worker got ownership_generation={ownership_generation!r}, want {self._generation!r}"
        )
        assert source is None, f"send_owned_worker got source={source!r}"
        self.send_calls.append((worker_id, msg, browser_ws, ownership_generation))
        return True, None


async def test_http_inspect_control_owner_forwards_the_exact_message() -> None:
    """Kills every one of ``_handle_http_inspect_control``'s 14 mutants
    (mutmut_1-14): mutmut_1 forces ``generation`` to ``None`` unconditionally
    (skipping ``capture_browser_ownership`` and the forward) -- caught
    because both calls must happen; mutmut_2/3 swap ``worker_id``/``ws`` for
    ``None`` in the ``capture_browser_ownership`` call -- caught by that
    call's argument asserts; mutmut_4/5 drop one of those two arguments,
    shifting arity to 1 positional against a 2-positional signature -- caught
    by the resulting ``TypeError``; mutmut_6 flips ``is not None`` to
    ``is None``, which for this owner scenario (a real generation, not
    ``None``) skips the forward entirely -- caught because it must have been
    sent; mutmut_7/8/9/10 each replace one ``send_owned_worker`` argument
    (``worker_id``/``msg``/``browser_ws``/``ownership_generation``) with
    ``None`` -- caught by that call's own argument asserts; mutmut_11/12 drop
    the ``msg`` argument entirely (by omitting the positional or by omitting
    both ``worker_id`` and ``msg_b`` and passing only ``worker_id``), which
    shifts or breaks arity against the required ``(worker_id, msg)``
    positional pair -- caught by the resulting ``TypeError``; mutmut_13/14
    drop the ``browser_ws``/``ownership_generation`` keyword entirely, which
    falls back to that parameter's own ``None`` default -- caught by the same
    argument asserts as mutmut_9/10."""
    ws = _WS()
    hub = _InspectHub(ws, generation=GENERATION)
    await _handle_http_inspect_control(hub, ws, WID, MSG_B)
    assert hub.capture_calls == [(WID, ws)]
    assert hub.send_calls == [(WID, MSG_B, ws, GENERATION)]


async def test_http_inspect_control_non_owner_forwards_nothing() -> None:
    """A non-owner (``capture_browser_ownership`` returns ``None``) has its
    control message dropped, not forwarded to the worker. Also kills
    mutmut_6 from the other direction: its flipped ``is None`` check would
    wrongly forward here, where the real function must not."""
    ws = _WS()
    hub = _InspectHub(ws, generation=None)
    await _handle_http_inspect_control(hub, ws, WID, MSG_B)
    assert hub.capture_calls == [(WID, ws)]
    assert hub.send_calls == []
