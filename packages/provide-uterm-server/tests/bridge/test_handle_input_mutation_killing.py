#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``browser_handlers._handle_input``.

Kill-suite only — behavioural coverage of browser input lives in
``test_browser_handlers_*.py``. Those suites never assert ``_handle_input``'s
return value, drive it through ``AsyncMock`` hubs that answer whatever they are
called with, and never reach the policy-gate hold path or the multi-part send
at all: under mutmut 3.8 (2026-09-23) ``_handle_input`` had 318 survivors,
including ``request = None`` and ``ok, error = None``, which crash on any
execution.

Everything here drives ``_handle_input`` directly against strict fakes: the
hub, gate and approval store raise on any unexpected argument, the clock and
uuid are pinned so every timestamp and request id is exact, and the module
logger is replaced so the policy-block debug line is asserted verbatim.

Measured under mutmut 3.8 on 2026-09-23 with this suite selected:
``_handle_input`` went from 318 survivors to exactly the 14 equivalents below,
and ``browser_handlers.py`` as a whole from 38.33 to 66.10 (745 of 1,127
killed). The file joined the perimeter once every handler was closed.

Documented equivalents (listed in ``mutation_equivalents.toml``):

- mutmut_8, 12, 13, 14: the ``cast("BrowserInputFrame", ...)`` type string
  mangled. ``typing.cast`` returns its second argument unchanged at runtime.
- mutmut_102-106 and 338-342: the ``reserved_sender`` branch's
  ``error = None if ok else "no_worker"`` forced to ``None``, forced to
  ``"no_worker"``, or given a mangled literal. ``error`` is only ever compared
  with ``"invalid_owner"``, which none of those values equal, and ``ok`` alone
  decides the send-failed branch, so every variant takes the same path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from provide.uterm.control_channel import encode_control_frame
from provide.uterm.server.bridge.frames import make_error_frame
from provide.uterm.server.bridge.hub.approvals import ApprovalRequest, ApprovalStatus
from provide.uterm.server.bridge.hub.ext import NoOpPolicyGate, PolicyDecision
from provide.uterm.server.bridge.routes import browser_handlers
from provide.uterm.server.bridge.routes.browser_handlers import _handle_input

WID = "input-worker"
NOW = 1000.0
UUID = "11111111-2222-3333-4444-555555555555"
GENERATION = 7
CLIENT_ID = 42  # an int, so ``str(context.client_id)`` is observable


class _WS:
    """A browser socket that records every frame sent to it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        assert isinstance(text, str), f"{self.name}.send_text got {text!r}"
        self.sent.append(text)

    def __repr__(self) -> str:
        return f"_WS({self.name})"


def _error(text: str) -> str:
    return encode_control_frame(make_error_frame(text))


def _pending(command: str, request_id: str, expires_at: float) -> str:
    return encode_control_frame(
        {"type": "approval_pending", "command": command, "request_id": request_id, "expires_at": expires_at}
    )


class _Gate:
    """A policy gate that answers from a script and checks every argument."""

    def __init__(self, context: Any, *decisions: PolicyDecision) -> None:
        self._context = context
        self._decisions = list(decisions)
        self.calls: list[str] = []

    async def intercept_input(self, data: str, context: Any) -> PolicyDecision:
        assert isinstance(data, str), f"intercept_input got data={data!r}"
        assert context is self._context, f"intercept_input got context={context!r}"
        self.calls.append(data)
        return self._decisions.pop(0) if self._decisions else PolicyDecision(action="allow")


@dataclass
class _Store:
    accept: bool = True
    added: list[ApprovalRequest] = field(default_factory=list)

    def add(self, request: ApprovalRequest) -> bool:
        assert isinstance(request, ApprovalRequest), f"approval_store.add got {request!r}"
        self.added.append(request)
        return self.accept


class _Hub:
    """A hub whose collaborators raise on any unexpected argument."""

    def __init__(
        self,
        ws: _WS,
        *,
        gate: Any = None,
        generation: int | None = GENERATION,
        send_result: tuple[bool, str | None] = (True, None),
        command: str | None = None,
        max_input_chars: int = 1000,
        max_buffer_chars: int = 1000,
        others: tuple[_WS, ...] = (),
    ) -> None:
        self.ws = ws
        self.context = SimpleNamespace(client_id=CLIENT_ID)
        self._policy_gate = gate if gate is not None else NoOpPolicyGate()
        self._generation = generation
        self._send_result = send_result
        self._command = command
        self.max_input_chars = max_input_chars
        self.max_buffer_chars = max_buffer_chars
        self._paused_browsers: set[Any] = set()
        self._hold_buffers: dict[Any, str] = {}
        self._input_buffers: dict[Any, str] = {}
        self.approval_store = _Store()
        self.browsers = {ws: None, **dict.fromkeys(others)}
        self.captures = 0
        self.contexts = 0
        self.buffered: list[str] = []
        self.sends: list[dict[str, Any]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    async def capture_browser_ownership(self, worker_id: str, ws: Any) -> int | None:
        assert (worker_id, ws) == (WID, self.ws), f"capture_browser_ownership got {(worker_id, ws)!r}"
        self.captures += 1
        return self._generation

    async def prepare_policy_context(self, ws: Any, worker_id: str, *, action: str) -> Any:
        assert (ws, worker_id, action) == (self.ws, WID, "input"), (
            f"prepare_policy_context got {(ws, worker_id, action)!r}"
        )
        self.contexts += 1
        return self.context

    async def send_owned_worker(
        self,
        worker_id: str,
        msg: dict[str, Any],
        *,
        browser_ws: Any,
        ownership_generation: int,
        source: Any,
    ) -> tuple[bool, str | None]:
        assert worker_id == WID, f"send_owned_worker got worker_id={worker_id!r}"
        assert browser_ws is self.ws, f"send_owned_worker got browser_ws={browser_ws!r}"
        assert source is self.ws, f"send_owned_worker got source={source!r}"
        assert ownership_generation == self._expected_generation, (
            f"send_owned_worker got ownership_generation={ownership_generation!r}"
        )
        self.sends.append(msg)
        return self._send_result

    _expected_generation: int | None = GENERATION

    async def append_event(self, worker_id: str, event: str, payload: dict[str, Any]) -> None:
        self.events.append((worker_id, event, payload))

    def _buffer_and_get_command(self, ws: Any, data: str) -> str | None:
        assert ws is self.ws, f"_buffer_and_get_command got ws={ws!r}"
        assert isinstance(data, str), f"_buffer_and_get_command got data={data!r}"
        self.buffered.append(data)
        return self._command

    async def _get(self, worker_id: str) -> Any:
        assert worker_id == WID, f"_get got worker_id={worker_id!r}"
        return SimpleNamespace(browsers=self.browsers)


class _Logger:
    def __init__(self) -> None:
        self.debug_calls: list[tuple[Any, ...]] = []

    def debug(self, *args: Any, **kwargs: Any) -> None:
        assert not kwargs, f"logger.debug got kwargs {kwargs!r}"
        self.debug_calls.append(args)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    """Fix the clock and uuid, and capture the module logger."""
    monkeypatch.setattr(browser_handlers, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(browser_handlers, "uuid", SimpleNamespace(uuid4=lambda: UUID))
    log = _Logger()
    monkeypatch.setattr(browser_handlers, "logger", log)
    return log


def _gated(ws: _WS, *decisions: PolicyDecision, **kw: Any) -> _Hub:
    hub = _Hub(ws, **kw)
    hub._policy_gate = _Gate(hub.context, *decisions)
    return hub


BLOCK_FMT = "input_blocked_by_policy worker_id=%s action=%s reason=%s part=%s"


# ---------------------------------------------------------------------------
# Empty input, the pause hold buffer, ownership and the length cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("msg", [{"type": "input"}, {"type": "input", "data": ""}])
async def test_empty_input_is_ignored_without_touching_the_hub(msg: dict[str, Any]) -> None:
    """Kills mutmut_5/7/17 (a missing ``data`` defaulting to ``None``/``"XXXX"``,
    which ``str()`` turns into non-empty input) and 19/20 (the return literal)."""
    ws = _WS("owner")
    hub = _Hub(ws)
    assert await _handle_input(hub, ws, WID, msg) == "ignored"
    assert (hub.captures, hub.sends, ws.sent) == (0, [], [])


async def test_a_paused_browser_appends_to_its_hold_buffer_by_default() -> None:
    """Kills mutmut_1 (``bypass_pause`` defaulting True), 23 (``not`` dropped),
    24-30 (the hold lookup/concatenation), 40 (the stored value) and 41/42."""
    ws = _WS("owner")
    hub = _Hub(ws)
    hub._paused_browsers.add(ws)
    hub._hold_buffers[ws] = "ab"
    hub._hold_buffers[None] = "WRONG"
    hub._hold_buffers[""] = "WRONG"
    assert await _handle_input(hub, ws, WID, {"data": "cd"}) == "buffered"
    assert hub._hold_buffers[ws] == "abcd"
    assert (hub.captures, hub.sends) == (0, [])


async def test_a_paused_browser_with_no_hold_buffer_starts_one() -> None:
    """Kills mutmut_27/29 (a missing hold buffer defaulting to ``None``)."""
    ws = _WS("owner")
    hub = _Hub(ws)
    hub._paused_browsers.add(ws)
    assert await _handle_input(hub, ws, WID, {"data": "cd"}) == "buffered"
    assert hub._hold_buffers == {ws: "cd"}


async def test_bypass_pause_sends_even_while_paused() -> None:
    """The other half of mutmut_23: with ``bypass_pause=True`` the hold buffer
    is skipped and the input is sent."""
    ws = _WS("owner")
    hub = _Hub(ws)
    hub._paused_browsers.add(ws)
    assert await _handle_input(hub, ws, WID, {"data": "x"}, bypass_pause=True) == "sent"
    assert hub._hold_buffers == {}


async def test_a_hold_buffer_exactly_at_the_cap_is_kept() -> None:
    """Kills mutmut_31 (``>`` -> ``>=``)."""
    ws = _WS("owner")
    hub = _Hub(ws, max_buffer_chars=4)
    hub._paused_browsers.add(ws)
    hub._hold_buffers[ws] = "ab"
    assert await _handle_input(hub, ws, WID, {"data": "cd"}) == "buffered"
    assert hub._hold_buffers[ws] == "abcd"


async def test_a_hold_buffer_past_the_cap_is_refused_with_the_exact_error() -> None:
    """Kills mutmut_32-39: the error frame and the ``"blocked"`` literal."""
    ws = _WS("owner")
    hub = _Hub(ws, max_buffer_chars=4)
    hub._paused_browsers.add(ws)
    hub._hold_buffers[ws] = "ab"
    assert await _handle_input(hub, ws, WID, {"data": "cde"}) == "blocked"
    assert ws.sent == [_error("Input too long.")]
    assert hub._hold_buffers[ws] == "ab"


async def test_an_ownership_override_is_used_instead_of_a_capture() -> None:
    """Kills mutmut_43 (the override discarded, so the hub is asked instead)."""
    ws = _WS("owner")
    hub = _Hub(ws, generation=99)
    hub._expected_generation = 5
    assert await _handle_input(hub, ws, WID, {"data": "x"}, ownership_generation_override=5) == "sent"
    assert hub.captures == 0


async def test_a_non_owner_is_refused() -> None:
    """Kills mutmut_51/52 (the ``"invalid_owner"`` literal)."""
    ws = _WS("viewer")
    hub = _Hub(ws, generation=None)
    assert await _handle_input(hub, ws, WID, {"data": "x"}) == "invalid_owner"
    assert (hub.sends, ws.sent) == ([], [])


async def test_input_past_the_length_cap_is_refused() -> None:
    """Kills mutmut_60/61 (the ``"blocked"`` literal on the length cap)."""
    ws = _WS("owner")
    hub = _Hub(ws, max_input_chars=3)
    assert await _handle_input(hub, ws, WID, {"data": "abcd"}) == "blocked"
    assert ws.sent == [_error("Input too long.")]
    assert hub.sends == []


# ---------------------------------------------------------------------------
# The no-op gate fast path
# ---------------------------------------------------------------------------


async def test_the_fast_path_sends_the_exact_frame_and_records_the_event() -> None:
    """Kills mutmut_62-70 (the policy-context call), 86/87 (the ``ts`` key),
    93/94/98/99 (``send_owned_worker`` arguments), 121-135 (the event,
    including the 120-character ``keys`` cut) and 136/137."""
    ws = _WS("owner")
    hub = _Hub(ws)
    data = "k" * 121
    assert await _handle_input(hub, ws, WID, {"data": data}) == "sent"
    assert hub.contexts == 1
    assert hub.sends == [{"type": "input", "data": data, "ts": NOW}]
    assert hub.events == [(WID, "input_send", {"owner": "dashboard_ws", "keys": "k" * 120})]
    assert ws.sent == []


async def test_the_fast_path_hands_the_frame_to_a_reserved_sender() -> None:
    """Kills mutmut_100/101: the reserved sender gets the exact frame and its
    ``True`` means sent; ``send_owned_worker`` is never called."""
    ws = _WS("owner")
    hub = _Hub(ws)
    got: list[Any] = []

    async def sender(msg: dict[str, Any]) -> bool:
        got.append(msg)
        return True

    assert await _handle_input(hub, ws, WID, {"data": "x"}, reserved_sender=sender) == "sent"
    assert got == [{"type": "input", "data": "x", "ts": NOW}]
    assert hub.sends == []


async def test_the_fast_path_reports_a_failed_reserved_send() -> None:
    """Kills mutmut_119/120 and the lost-connection frame."""
    ws = _WS("owner")
    hub = _Hub(ws)

    async def sender(_msg: dict[str, Any]) -> bool:
        return False

    assert await _handle_input(hub, ws, WID, {"data": "x"}, reserved_sender=sender) == "send_failed"
    assert ws.sent == [_error("Worker connection lost.")]
    assert hub.events == []


async def test_the_fast_path_passes_through_a_lost_ownership() -> None:
    """Kills mutmut_108-111: ``invalid_owner`` from the hub is returned as-is,
    with no error frame."""
    ws = _WS("owner")
    hub = _Hub(ws, send_result=(False, "invalid_owner"))
    assert await _handle_input(hub, ws, WID, {"data": "x"}) == "invalid_owner"
    assert (ws.sent, hub.events) == ([], [])


# ---------------------------------------------------------------------------
# The gated path: which chunks go to the gate first
# ---------------------------------------------------------------------------


async def test_a_partial_chunk_is_checked_then_buffered() -> None:
    """Kills mutmut_62 (context forced None), 72/75/77 (``is_complete_chunk``),
    139/140, 141-145 (the first ``intercept_input`` call), 221-225 (the buffer
    call) and 226-228."""
    ws = _WS("owner")
    hub = _gated(ws, command=None)
    assert await _handle_input(hub, ws, WID, {"data": "ls"}) == "buffered"
    assert hub._policy_gate.calls == ["ls"]
    assert hub.buffered == ["ls"]
    assert hub.sends == []


async def test_a_partial_chunk_into_an_open_line_skips_the_first_check() -> None:
    """Kills mutmut_138 (``and`` -> ``or``): a browser already mid-line is not
    re-checked chunk by chunk."""
    ws = _WS("owner")
    hub = _gated(ws, command=None)
    hub._input_buffers[ws] = "l"
    assert await _handle_input(hub, ws, WID, {"data": "s"}) == "buffered"
    assert hub._policy_gate.calls == []


@pytest.mark.parametrize("data", ["ls\r", "ls\n"])
async def test_a_complete_single_command_is_checked_once_as_typed(data: str) -> None:
    """Kills mutmut_73/74/76 (either line ending alone must count as complete)
    and 232/234 (a single part is replaced by the raw command, which the
    splitter would have stripped)."""
    ws = _WS("owner")
    hub = _gated(ws, command=data)
    assert await _handle_input(hub, ws, WID, {"data": data}) == "sent"
    assert hub._policy_gate.calls == [data]
    assert hub.buffered == [data]


async def test_every_part_of_a_chained_command_is_checked_and_the_whole_is_sent() -> None:
    """Kills mutmut_229-231 (the splitter), 233 (``<= 1`` -> ``<= 2``),
    235-239 (the per-part call), 315-335 (the send), 357-371 (the event) and
    372/373."""
    ws = _WS("owner")
    command = "a; " + "b" * 130 + "\r"
    hub = _gated(ws, command=command)
    assert await _handle_input(hub, ws, WID, {"data": command}) == "sent"
    assert hub._policy_gate.calls == ["a", "b" * 130]
    assert hub.sends == [{"type": "input", "data": command, "ts": NOW}]
    assert hub.events == [(WID, "input_send", {"owner": "dashboard_ws", "keys": command[:120]})]


# ---------------------------------------------------------------------------
# The gated path: hold for approval
# ---------------------------------------------------------------------------


def _expected_request(request_id: str, command: str, ws: _WS) -> ApprovalRequest:
    return ApprovalRequest(
        id=request_id,
        worker_id=WID,
        submitter_id=str(CLIENT_ID),
        command=command,
        status=ApprovalStatus.PENDING,
        created_at=NOW,
        expires_at=NOW + 30,
        origin_browser=ws,
        ownership_generation=GENERATION,
    )


@pytest.mark.parametrize(("given", "expected_id"), [("req-1", "req-1"), (None, UUID)])
async def test_a_held_chunk_files_the_exact_request_and_tells_every_browser(
    given: str | None, expected_id: str
) -> None:
    """Kills mutmut_146-172 (the decision check and every ``ApprovalRequest``
    field, including a given id kept and a missing one minted), 174, 183-198
    (the pause, the lookup and the ``approval_pending`` frame to each browser)
    and 199/200."""
    ws, other = _WS("owner"), _WS("other")
    hub = _gated(ws, PolicyDecision(action="hold", request_id=given, timeout_s=30), others=(other,))
    assert await _handle_input(hub, ws, WID, {"data": "rm"}) == "held"
    assert hub.approval_store.added == [_expected_request(expected_id, "rm", ws)]
    assert hub._paused_browsers == {ws}
    frame = _pending("rm", expected_id, NOW + 30)
    assert (ws.sent, other.sent) == ([frame], [frame])
    assert hub.buffered == []


async def test_a_held_chunk_with_a_colliding_id_is_refused() -> None:
    """Kills mutmut_173 and 175-182: the collision frame and literal, and the
    browser is not paused."""
    ws = _WS("owner")
    hub = _gated(ws, PolicyDecision(action="hold", request_id="dup", timeout_s=30))
    hub.approval_store.accept = False
    assert await _handle_input(hub, ws, WID, {"data": "rm"}) == "collision"
    assert ws.sent == [_error("Approval request ID collision.")]
    assert hub._paused_browsers == set()


@pytest.mark.parametrize(("given", "expected_id"), [("req-2", "req-2"), (None, UUID)])
async def test_a_held_part_files_the_whole_command(given: str | None, expected_id: str) -> None:
    """Kills mutmut_240-266 (the per-part hold and its ``ApprovalRequest``,
    which carries the whole command, not the part), 268 and 277-294."""
    ws, other = _WS("owner"), _WS("other")
    command = "ls; rm\r"
    hub = _gated(
        ws,
        PolicyDecision(action="allow"),
        PolicyDecision(action="hold", request_id=given, timeout_s=30),
        command=command,
        others=(other,),
    )
    assert await _handle_input(hub, ws, WID, {"data": command}) == "held"
    assert hub._policy_gate.calls == ["ls", "rm"]
    assert hub.approval_store.added == [_expected_request(expected_id, command, ws)]
    assert hub._paused_browsers == {ws}
    frame = _pending(command, expected_id, NOW + 30)
    assert (ws.sent, other.sent) == ([frame], [frame])
    assert hub.sends == []


async def test_a_held_part_with_a_colliding_id_is_refused() -> None:
    """Kills mutmut_267 and 269-276."""
    ws = _WS("owner")
    hub = _gated(ws, PolicyDecision(action="hold", request_id="dup", timeout_s=30), command="rm\r")
    hub.approval_store.accept = False
    assert await _handle_input(hub, ws, WID, {"data": "rm\r"}) == "collision"
    assert ws.sent == [_error("Approval request ID collision.")]
    assert hub._paused_browsers == set()


# ---------------------------------------------------------------------------
# The gated path: deny
# ---------------------------------------------------------------------------


async def test_a_denied_chunk_is_blocked_and_logged_verbatim(_pinned: _Logger) -> None:
    """Kills mutmut_201-220: the allow check, the exact debug line and its
    arguments, the error frame and the literal."""
    ws = _WS("owner")
    hub = _gated(ws, PolicyDecision(action="deny", reason="no rm"))
    assert await _handle_input(hub, ws, WID, {"data": "rm"}) == "blocked"
    assert _pinned.debug_calls == [(BLOCK_FMT, WID, "deny", "no rm", "rm")]
    assert ws.sent == [_error("Command part blocked by policy: rm")]
    assert hub.buffered == []


async def test_a_denied_part_is_blocked_and_logged_verbatim(_pinned: _Logger) -> None:
    """Kills mutmut_295-314: the same for a part of a complete command. The
    frame and log name the part, not the whole command."""
    ws = _WS("owner")
    command = "ls; rm\r"
    hub = _gated(ws, PolicyDecision(action="allow"), PolicyDecision(action="deny", reason="no rm"), command=command)
    assert await _handle_input(hub, ws, WID, {"data": command}) == "blocked"
    assert _pinned.debug_calls == [(BLOCK_FMT, WID, "deny", "no rm", "rm")]
    assert ws.sent == [_error("Command part blocked by policy: rm")]
    assert hub.sends == []


# ---------------------------------------------------------------------------
# The gated path: send outcomes
# ---------------------------------------------------------------------------


async def test_the_gated_path_hands_the_command_to_a_reserved_sender() -> None:
    """Kills mutmut_324 (the sender check inverted) and 336/337."""
    ws = _WS("owner")
    hub = _gated(ws, command="ls\r")
    got: list[Any] = []

    async def sender(msg: dict[str, Any]) -> bool:
        got.append(msg)
        return True

    assert await _handle_input(hub, ws, WID, {"data": "ls\r"}, reserved_sender=sender) == "sent"
    assert got == [{"type": "input", "data": "ls\r", "ts": NOW}]
    assert hub.sends == []


async def test_the_gated_path_reports_a_failed_send() -> None:
    """Kills mutmut_348-356: the lost-connection frame and literal."""
    ws = _WS("owner")
    hub = _gated(ws, command="ls\r", send_result=(False, None))
    assert await _handle_input(hub, ws, WID, {"data": "ls\r"}) == "send_failed"
    assert ws.sent == [_error("Worker connection lost.")]
    assert hub.events == []


async def test_the_gated_path_passes_through_a_lost_ownership() -> None:
    """Kills mutmut_343-347."""
    ws = _WS("owner")
    hub = _gated(ws, command="ls\r", send_result=(False, "invalid_owner"))
    assert await _handle_input(hub, ws, WID, {"data": "ls\r"}) == "invalid_owner"
    assert (ws.sent, hub.events) == ([], [])
