#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers.handle_browser_message`` itself.

Kill-suite only -- behavioural coverage of the dispatcher lives in
``test_browser_handlers_coverage.py`` and ``test_browser_handlers_mutations.py``.
Those suites drive ``handle_browser_message`` through a real ``TermHub`` with
``MagicMock``/``AsyncMock`` websockets, which is why a batch of dispatcher-only
mutants survived:

- Neither suite sends an ``analyze_req`` message at all, so the whole branch
  (its string match and its three-argument call to ``_handle_analyze_req``)
  is unexercised.
- The ``snapshot_req`` tests assert on the *worker* socket's outbound send
  through the real hub, not on the arguments ``handle_browser_message`` itself
  passes to ``_handle_snapshot_req`` -- a wrong argument there can still end
  up producing the same worker-visible effect, given the specific hub state
  each test sets up, so the argument-level mutant survives even though the
  test passes.
- The ping test decodes the outbound frame and checks ``sent["type"] ==
  "pong"`` and that a ``"ts"`` key exists, but never pins the clock or checks
  the exact ``ts`` value, and never makes ``send_text`` fail -- so neither a
  mangled ``ts`` argument nor a broken ``suppress(...)`` is visible.
- Neither suite sends any of the ``_HTTP_INSPECT_CONTROL_TYPES`` messages, so
  that branch's membership check and its four-argument call are unexercised.

Everything here drives ``handle_browser_message`` directly, replacing each
``_handle_*`` collaborator it dispatches to with a strict recorder that
asserts the *exact* positional-argument tuple it received -- so a ``None``
swapped in for one argument, or one argument dropped entirely (which changes
the tuple's length), shows up as an assertion failure instead of silently
succeeding. The module clock is pinned so the ping branch's ``ts`` is exact.

No equivalents were found: every mutant in
``scratchpad/diffs_handle_browser_message.txt`` (ids 9, 15-22, 68, 71, 83-91)
is killed below by a distinguishing input.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import make_pong_frame
from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import (
    _HTTP_INSPECT_CONTROL_TYPES,
    handle_browser_message,
)

WID = "browser-worker"
NOW = 1000.0
HUB: Any = SimpleNamespace(marker="hub")
WS: Any = SimpleNamespace(marker="ws")


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``browser_handlers.time`` so the ping branch's ``ts`` is exact."""
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW))


class _Recorder:
    """A strict async stand-in for one of the ``_handle_*`` collaborators.

    Asserts the exact positional-argument tuple it is called with. A ``None``
    swapped in for one argument, or one argument dropped (which shortens the
    tuple), fails the assertion instead of silently succeeding.
    """

    def __init__(self, expected: tuple[Any, ...]) -> None:
        self._expected = expected
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any) -> None:
        assert args == self._expected, f"called with {args!r}, expected {self._expected!r}"
        self.calls.append(args)


class _WS:
    """A browser socket whose ``send_text`` can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self._fail = fail

    async def send_text(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("send failed")
        assert isinstance(text, str), f"send_text got {text!r}"
        self.sent.append(text)


# ---------------------------------------------------------------------------
# snapshot_req
# ---------------------------------------------------------------------------


async def test_snapshot_req_reaches_its_handler_with_every_argument_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_9 (``ws`` replaced by ``None`` in the ``_handle_snapshot_req``
    call): the recorder only accepts the real ``ws``, and ``owned_hijack`` is
    untouched by this branch either way."""
    recorder = _Recorder((HUB, WS, WID))
    monkeypatch.setattr(browser_handlers, "_handle_snapshot_req", recorder)
    result = await handle_browser_message(HUB, WS, WID, "operator", {"type": "snapshot_req"}, True)
    assert result is True
    assert recorder.calls == [(HUB, WS, WID)]


# ---------------------------------------------------------------------------
# analyze_req
# ---------------------------------------------------------------------------


async def test_analyze_req_reaches_its_handler_with_every_argument_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_15/16 (the ``"analyze_req"`` literal mangled to
    ``"XXanalyze_reqXX"``/``"ANALYZE_REQ"``, so the branch never matches and
    the recorder is never called -- caught by ``recorder.calls == [...]``)
    and mutmut_17-22 (``hub``/``ws``/``worker_id`` replaced by ``None``, or
    one of the three dropped entirely, shortening the argument tuple)."""
    recorder = _Recorder((HUB, WS, WID))
    monkeypatch.setattr(browser_handlers, "_handle_analyze_req", recorder)
    result = await handle_browser_message(HUB, WS, WID, "operator", {"type": "analyze_req"}, False)
    assert result is False
    assert recorder.calls == [(HUB, WS, WID)]


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


async def test_ping_sends_the_exact_pong_frame_from_the_pinned_clock() -> None:
    """Kills mutmut_71 (``ts=time.time()`` replaced by ``ts=None``): with
    ``ts=None``, ``make_pong_frame`` falls back to its own, unpinned
    ``time.time()`` call inside ``frames.py``, producing a frame whose ``ts``
    will never match the one built here from the pinned clock."""
    ws = _WS()
    result = await handle_browser_message(HUB, ws, WID, "viewer", {"type": "ping"}, True)
    assert result is True
    expected = encode_control_frame(make_pong_frame(ts=NOW))
    assert ws.sent == [expected]


async def test_ping_swallows_a_failed_send_text() -> None:
    """Kills mutmut_68 (``suppress(Exception)`` replaced by ``suppress(None)``):
    once ``send_text`` actually raises, passing ``None`` as the exception
    class makes ``contextlib.suppress.__exit__`` itself raise ``TypeError``
    (``issubclass() arg 2 must be a class...``), so the failure is no longer
    swallowed and escapes ``handle_browser_message`` -- which is exactly what
    this test asserts does not happen."""
    ws = _WS(fail=True)
    result = await handle_browser_message(HUB, ws, WID, "viewer", {"type": "ping"}, False)
    assert result is False
    assert ws.sent == []


# ---------------------------------------------------------------------------
# _HTTP_INSPECT_CONTROL_TYPES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mtype", sorted(_HTTP_INSPECT_CONTROL_TYPES))
async def test_http_inspect_control_types_reach_their_handler_with_every_argument_exact(
    monkeypatch: pytest.MonkeyPatch, mtype: str
) -> None:
    """Kills mutmut_84-87 (``hub``/``ws``/``worker_id``/``msg_b`` replaced by
    ``None``) and 88-91 (one of the four dropped, shortening the argument
    tuple). Parametrized over every member of ``_HTTP_INSPECT_CONTROL_TYPES``
    so none of the three literals goes unexercised. Also contributes to
    killing mutmut_83 (see below): under ``not in``, a real member of the set
    would no longer reach the handler at all, so ``recorder.calls`` would stay
    empty here."""
    msg_b = {"type": mtype, "marker": "distinct"}
    recorder = _Recorder((HUB, WS, WID, msg_b))
    monkeypatch.setattr(browser_handlers, "_handle_http_inspect_control", recorder)
    result = await handle_browser_message(HUB, WS, WID, "operator", msg_b, True)
    assert result is True
    assert recorder.calls == [(HUB, WS, WID, msg_b)]


async def test_an_unrelated_message_type_never_reaches_the_http_inspect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills mutmut_83 (``elif mtype in _HTTP_INSPECT_CONTROL_TYPES:`` inverted
    to ``not in``): a type genuinely absent from the set would incorrectly
    reach the handler under the inverted check. Together with the
    parametrized test above (a real member *does* reach the handler), this
    pins the membership check in both directions."""
    recorder = _Recorder(())
    monkeypatch.setattr(browser_handlers, "_handle_http_inspect_control", recorder)
    msg_b = {"type": "totally_unrelated_type"}
    result = await handle_browser_message(HUB, WS, WID, "operator", msg_b, False)
    assert result is False
    assert recorder.calls == []
