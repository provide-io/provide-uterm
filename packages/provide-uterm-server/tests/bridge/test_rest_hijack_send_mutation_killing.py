#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for ``rest._hijack_send``.

Kill-suite only -- behavioural coverage of ``POST
/worker/{id}/hijack/{hid}/send`` lives in ``test_routes.py`` and friends
(``test_routes_advanced.py``, ``test_owned_input_fencing.py``,
``test_telemetry_emission_sites.py``). Those suites drive the route through a
real ``TestClient`` against a real ``TermHub``, so they only ever observe the
HTTP status code and a loosely-shaped JSON body for the handful of scenarios
they set up (missing session, empty keys). They never pin: the exact
``_client_id`` fallback/derivation from ``http_request.client``; the exact
``hub.metric``/``logger.warning``/``hub.emit_telemetry`` calls on the rate-limit
path; the exact literal error strings and status codes on every branch; the
exact keyword arguments threaded into ``hub.wait_for_guard`` and
``hub.send_owned_worker``; the ``"invalid_owner"`` string comparison that picks
404 over the generic 409; the exact ``hub.append_event`` payload (including
the 120-character ``keys`` truncation); the three positional arguments to
``hub.get_fresh_hijack_expiry``; or the field names/values of the final
success dict. ``_hijack_send`` was pulled out of the ``@router.post``-decorated
closure it used to live in (mutmut skips decorated functions -- see
``docs/mutmut-survivors-triage.md`` Wave 9), so none of that had ever been
measured; under mutmut 3.8 (2026-09-23) it had 119 survivors.

Every hub collaborator below is a strict fake mirroring the real
``TermHub`` signatures (see
``packages/provide-uterm-server/src/provide/uterm/server/bridge/hub/core_impl.py``:
``allow_rest_send_for``, ``metric``, ``emit_telemetry``, ``get_rest_session``,
``max_input_chars``, ``wait_for_guard``, ``send_owned_worker``,
``append_event``, ``get_fresh_hijack_expiry``). None use
``AsyncMock(return_value=...)``: a mock configured that way would still
return its canned value after an argument was silently dropped or replaced
with ``None``, so it cannot distinguish the real call from a mutant's call.
Each fake asserts (or records for the test to assert) the exact arguments it
receives. The module ``logger`` is replaced with a recorder that pins the
exact format string and every positional substitution argument. ``time`` is
monkeypatched so ``time.time() == time.monotonic() == NOW``, which makes
``_mono_to_wall(x) == x`` exactly -- so the ``ts`` written into the input
frame and the ``lease_expires_at`` echoed back are pinned to exact floats
supplied by the fakes, not derived from the real clock.

No documented equivalents were needed: every one of the 119 survivors listed
in the mutation report is killed below by a distinguishing input.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import JSONResponse

from provide.uterm.server.bridge.models import HijackSendRequest
from provide.uterm.server.bridge.routes import rest
from provide.uterm.server.bridge.routes.rest import _hijack_send

WID = "w1"
HID = "h1"
NOW = 1_700_000_000.5


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``time.time()`` and ``time.monotonic()`` to the same value so
    ``_mono_to_wall(x) == x`` exactly, making every timestamp/expiry
    assertion below an exact-value comparison instead of a range check."""
    monkeypatch.setattr(rest, "time", SimpleNamespace(time=lambda: NOW, monotonic=lambda: NOW))


class _Logger:
    """Strict stand-in for ``rest.logger``: records the exact positional
    call, since the source never passes keyword arguments to it."""

    def __init__(self) -> None:
        self.warning_calls: list[tuple[Any, ...]] = []
        self.info_calls: list[tuple[Any, ...]] = []

    def warning(self, *args: Any) -> None:
        self.warning_calls.append(args)

    def info(self, *args: Any) -> None:
        self.info_calls.append(args)


@pytest.fixture
def logger_recorder(monkeypatch: pytest.MonkeyPatch) -> _Logger:
    recorder = _Logger()
    monkeypatch.setattr(rest, "logger", recorder)
    return recorder


class _Req:
    """Minimal stand-in for FastAPI's ``Request``: ``_hijack_send`` reads
    only ``http_request.client.host``."""

    def __init__(self, client_host: str | None) -> None:
        self.client = SimpleNamespace(host=client_host) if client_host is not None else None


class _Hub:
    """Strict fake mirroring the exact ``TermHub`` methods ``_hijack_send``
    calls (signatures per ``hub/core_impl.py``). Fixed-by-test-suite
    invariants (``worker_id``/``hijack_id`` always the module constants, and
    the ``send_owned_worker``/``emit_telemetry`` parameters ``_hijack_send``
    never passes) are asserted inline; values that vary per test are
    recorded for the test body to assert exactly."""

    def __init__(
        self,
        *,
        expected_client_id: str,
        allow_send: bool = True,
        rest_session: Any = None,
        max_input_chars: int = 10_000,
        guard_result: tuple[bool, dict[str, Any] | None, str | None] = (True, None, None),
        send_result: tuple[bool, str | None] = (True, None),
        fresh_expires: float = 0.0,
    ) -> None:
        self._expected_client_id = expected_client_id
        self._allow_send = allow_send
        self._rest_session = rest_session
        self.max_input_chars = max_input_chars
        self._guard_result = guard_result
        self._send_result = send_result
        self._fresh_expires = fresh_expires

        self.allow_rest_send_for_calls: list[str] = []
        self.metric_calls: list[tuple[str, int]] = []
        self.telemetry_calls: list[dict[str, Any]] = []
        self.get_rest_session_calls: list[tuple[str, str]] = []
        self.wait_for_guard_calls: list[dict[str, Any]] = []
        self.send_owned_worker_calls: list[dict[str, Any]] = []
        self.append_event_calls: list[tuple[Any, Any, Any]] = []
        self.get_fresh_hijack_expiry_calls: list[tuple[Any, Any, Any]] = []

    def allow_rest_send_for(self, client_id: str) -> bool:
        assert client_id == self._expected_client_id, f"allow_rest_send_for got client_id={client_id!r}"
        self.allow_rest_send_for_calls.append(client_id)
        return self._allow_send

    def metric(self, name: str, value: int = 1) -> None:
        self.metric_calls.append((name, value))

    async def emit_telemetry(
        self,
        event_type: str,
        *,
        worker_id: str,
        principal: str | None = None,
        role: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert principal is None, f"emit_telemetry got principal={principal!r}"
        assert role is None, f"emit_telemetry got role={role!r}"
        self.telemetry_calls.append({"event_type": event_type, "worker_id": worker_id, "metadata": metadata})

    async def get_rest_session(self, worker_id: str, hijack_id: str) -> Any:
        self.get_rest_session_calls.append((worker_id, hijack_id))
        return self._rest_session

    async def wait_for_guard(
        self,
        worker_id: str,
        *,
        expect_prompt_id: str | None,
        expect_regex: str | None,
        timeout_ms: int,
        poll_interval_ms: int,
    ) -> tuple[bool, dict[str, Any] | None, str | None]:
        self.wait_for_guard_calls.append(
            {
                "worker_id": worker_id,
                "expect_prompt_id": expect_prompt_id,
                "expect_regex": expect_regex,
                "timeout_ms": timeout_ms,
                "poll_interval_ms": poll_interval_ms,
            }
        )
        return self._guard_result

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
        assert browser_ws is None, f"send_owned_worker got browser_ws={browser_ws!r}"
        assert ownership_generation is None, f"send_owned_worker got ownership_generation={ownership_generation!r}"
        assert source is None, f"send_owned_worker got source={source!r}"
        self.send_owned_worker_calls.append({"worker_id": worker_id, "msg": msg, "rest_hijack_id": rest_hijack_id})
        return self._send_result

    async def append_event(self, worker_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.append_event_calls.append((worker_id, event_type, data))
        return {}

    async def get_fresh_hijack_expiry(self, worker_id: str, hijack_id: str, fallback: float) -> float:
        self.get_fresh_hijack_expiry_calls.append((worker_id, hijack_id, fallback))
        return self._fresh_expires


def _request(**overrides: Any) -> HijackSendRequest:
    fields: dict[str, Any] = {
        "keys": "hi",
        "expect_prompt_id": None,
        "expect_regex": None,
        "timeout_ms": 2000,
        "poll_interval_ms": 120,
    }
    fields.update(overrides)
    return HijackSendRequest.model_validate(fields)


async def _run(hub: _Hub, http_request: _Req, request: HijackSendRequest) -> Any:
    return await asyncio.wait_for(_hijack_send(hub, http_request, WID, HID, request), timeout=2)


# ---------------------------------------------------------------------------
# Rate limiting: the ``_client_id`` derivation and the 429 branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client_host", "expected_client_id"),
    [("9.9.9.9", "9.9.9.9"), (None, "unknown")],
)
async def test_rate_limited_uses_derived_client_id_everywhere(
    logger_recorder: _Logger, client_host: str | None, expected_client_id: str
) -> None:
    """Kills mutmut_1-6 (the ``_client_id`` derivation: forced ``None``,
    ``or``->``and``, both ternary-condition short-circuits, and the
    ``"unknown"`` literal mangled/case-flipped -- the ``client_host=None``
    case additionally makes mutmut_4's forced-true condition dereference
    ``None.host`` and raise, since the real code only takes that branch when
    a client is present), mutmut_8 (``allow_rest_send_for`` argument forced
    ``None`` -- the fake asserts the exact value), mutmut_9-11 (the metric
    name), mutmut_13-15/20 (the ``logger.warning`` format string and all
    three substitution arguments) and mutmut_22-24/27-35 (the
    ``emit_telemetry`` call: event type, ``worker_id``, and every key/value
    of ``metadata``, including the trailing-comma arity mutant that merely
    removes the line without changing behaviour here since it is the last
    kwarg -- caught anyway because the whole ``metadata`` dict is compared).
    """
    hub = _Hub(expected_client_id=expected_client_id, allow_send=False)
    req = _Req(client_host)
    result = await _run(hub, req, _request())

    assert isinstance(result, JSONResponse)
    assert result.status_code == 429
    assert json.loads(result.body) == {"error": "rate_limited"}
    assert hub.allow_rest_send_for_calls == [expected_client_id]
    assert hub.metric_calls == [("rest_send_rate_limited_total", 1)]
    assert logger_recorder.warning_calls == [
        ("rest_send_rate_limited worker_id=%s hijack_id=%s client=%s", WID, HID, expected_client_id)
    ]
    assert hub.telemetry_calls == [
        {
            "event_type": "rate_limit.triggered",
            "worker_id": WID,
            "metadata": {"client_id": expected_client_id, "limit_type": "rest_send"},
        }
    ]
    # The rate limit short-circuits everything after it.
    assert hub.get_rest_session_calls == []
    assert hub.append_event_calls == []


# ---------------------------------------------------------------------------
# Missing/expired session and empty keys
# ---------------------------------------------------------------------------


async def test_missing_session_returns_exact_404() -> None:
    """Kills mutmut_51 (body forced ``None``) and mutmut_55-59 (the
    ``"error"``/``"Invalid or expired hijack session."`` literals mangled or
    case-flipped)."""
    hub = _Hub(expected_client_id="unknown", rest_session=None)
    result = await _run(hub, _Req(None), _request())

    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    assert json.loads(result.body) == {"error": "Invalid or expired hijack session."}


async def test_empty_keys_returns_exact_400() -> None:
    """Kills mutmut_68 (the ``"keys must not be empty."`` literal mangled)."""
    hub = _Hub(expected_client_id="unknown", rest_session=SimpleNamespace(lease_expires_at=0.0))
    result = await _run(hub, _Req(None), _request(keys=""))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 400
    assert json.loads(result.body) == {"error": "keys must not be empty."}


# ---------------------------------------------------------------------------
# The input-length cap: strictly-over vs exactly-at
# ---------------------------------------------------------------------------


async def test_keys_over_the_cap_returns_exact_400() -> None:
    """Kills mutmut_72-78: the whole error dict forced ``None``, the
    ``status_code`` forced ``None``/dropped/changed to 401, and the
    ``"error"`` key literal mangled/case-flipped. Uses distinct lengths (6
    vs cap 5) so the interpolated message is exact and non-symmetric."""
    hub = _Hub(expected_client_id="unknown", rest_session=SimpleNamespace(lease_expires_at=0.0), max_input_chars=5)
    result = await _run(hub, _Req(None), _request(keys="abcdef"))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 400
    assert json.loads(result.body) == {"error": "keys too long: 6 > 5"}
    assert hub.wait_for_guard_calls == []


async def test_keys_exactly_at_the_cap_is_allowed_through() -> None:
    """Kills mutmut_71 (``>`` -> ``>=``): with ``len(keys) ==
    max_input_chars`` the real ``>`` comparison is false and the request
    proceeds to a full success, while the ``>=`` mutant would reject it with
    the 400 from the previous test."""
    hub = _Hub(
        expected_client_id="unknown",
        rest_session=SimpleNamespace(lease_expires_at=42.0),
        max_input_chars=5,
        guard_result=(True, None, None),
        send_result=(True, None),
        fresh_expires=42.0,
    )
    result = await _run(hub, _Req(None), _request(keys="abcde"))

    assert result == {
        "ok": True,
        "worker_id": WID,
        "hijack_id": HID,
        "sent": "abcde",
        "matched_prompt_id": None,
        "lease_expires_at": 42.0,
    }


# ---------------------------------------------------------------------------
# The prompt-guard-not-satisfied branch (409)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "snapshot", "expected_error", "expected_prompt_id"),
    [
        ("custom_reason", {"prompt_detected": {"prompt_id": "cur-prompt"}}, "custom_reason", "cur-prompt"),
        (None, None, "prompt_guard_not_satisfied", None),
    ],
)
async def test_guard_not_matched_returns_exact_409(
    reason: str | None,
    snapshot: dict[str, Any] | None,
    expected_error: str,
    expected_prompt_id: str | None,
) -> None:
    """Kills mutmut_91/92/94 (body forced ``None``, ``status_code`` forced
    ``None``/dropped) and mutmut_103 (409 -> 410). The ``reason="custom_reason"``
    case kills mutmut_97 (``or``->``and``: a truthy ``reason`` would be
    replaced by the literal instead of passed through) and mutmut_95/96 (the
    ``"error"`` key). The ``reason=None`` case is the only one that reaches
    the ``"prompt_guard_not_satisfied"`` literal, killing mutmut_98/99. The
    non-``None`` snapshot kills mutmut_102 (``extract_prompt_id(snapshot)``
    forced to ``extract_prompt_id(None)``), and mutmut_100/101 kill the
    ``"current_prompt_id"`` key in both cases."""
    hub = _Hub(
        expected_client_id="unknown",
        rest_session=SimpleNamespace(lease_expires_at=0.0),
        guard_result=(False, snapshot, reason),
    )
    result = await _run(hub, _Req(None), _request())

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    assert json.loads(result.body) == {"error": expected_error, "current_prompt_id": expected_prompt_id}


# ---------------------------------------------------------------------------
# send_owned_worker outcomes: invalid_owner vs generic failure vs success
# ---------------------------------------------------------------------------


async def test_invalid_owner_from_send_returns_exact_404() -> None:
    """Kills mutmut_120/121 (the ``"invalid_owner"`` comparison literal
    case-flipped/mangled -- with a mismatched literal the comparison would
    be false and this ``ok=False`` result would instead fall into the
    generic "No worker connected" 409 branch, which is a different status
    code and body) and mutmut_122-131 (the returned dict forced ``None``,
    ``status_code`` forced ``None``/dropped/changed to 405, and the
    ``"error"``/``"Invalid or expired hijack session."`` literals
    mangled/case-flipped)."""
    hub = _Hub(
        expected_client_id="unknown",
        rest_session=SimpleNamespace(lease_expires_at=0.0),
        send_result=(False, "invalid_owner"),
    )
    result = await _run(hub, _Req(None), _request())

    assert isinstance(result, JSONResponse)
    assert result.status_code == 404
    assert json.loads(result.body) == {"error": "Invalid or expired hijack session."}


async def test_send_failure_without_invalid_owner_returns_exact_409() -> None:
    """Kills mutmut_133 (body forced ``None``) and mutmut_137-141 (the
    ``"error"``/``"No worker connected for this session."`` literals
    mangled/case-flipped). Uses ``error=None`` (not ``"invalid_owner"``) so
    this exercises the ``if not ok`` branch specifically, distinct from the
    previous test."""
    hub = _Hub(
        expected_client_id="unknown",
        rest_session=SimpleNamespace(lease_expires_at=0.0),
        send_result=(False, None),
    )
    result = await _run(hub, _Req(None), _request())

    assert isinstance(result, JSONResponse)
    assert result.status_code == 409
    assert json.loads(result.body) == {"error": "No worker connected for this session."}


# ---------------------------------------------------------------------------
# Full success path
# ---------------------------------------------------------------------------


async def test_success_pins_every_call_and_the_exact_response(logger_recorder: _Logger) -> None:
    """The comprehensive happy-path test. Kills:

    - mutmut_80-84: every ``wait_for_guard`` keyword argument
      (``worker_id``, ``expect_prompt_id``, ``expect_regex``, ``timeout_ms``,
      ``poll_interval_ms``) forced ``None`` -- distinct, non-``None`` values
      are used for each so a dropped/nulled argument is caught by the exact
      recorded-call comparison.
    - mutmut_111-114/117/118: the ``send_owned_worker`` input frame's
      ``"type"``/``"data"``/``"ts"`` keys mangled/case-flipped -- ``ts`` is
      pinned via the frozen clock fixture.
    - mutmut_144-146/153: the ``logger.info`` format string and all three
      of ``worker_id``/``hijack_id``/``client_id`` forced ``None``.
    - mutmut_155-171: every argument to ``append_event`` (``worker_id``,
      the ``"hijack_send"`` event type, and the whole payload dict including
      the 120-character ``keys`` truncation) forced ``None``/dropped/mangled
      -- a 130-character ``keys`` value makes the truncation to 120 vs 121
      characters (mutmut_167) observable.
    - mutmut_173-175: all three positional arguments to
      ``get_fresh_hijack_expiry`` forced ``None``.
    - mutmut_179-192: every key and value of the final success dict
      (``ok``, ``worker_id``, ``hijack_id``, ``sent``, ``matched_prompt_id``
      -- including ``extract_prompt_id`` forced to read ``None`` instead of
      the real snapshot -- and ``lease_expires_at``).
    """
    keys = "k" * 130
    snapshot = {"prompt_detected": {"prompt_id": "matched-prompt"}}
    hub = _Hub(
        expected_client_id="unknown",
        rest_session=SimpleNamespace(lease_expires_at=555.0),
        max_input_chars=1000,
        guard_result=(True, snapshot, None),
        send_result=(True, None),
        fresh_expires=999.25,
    )
    request = _request(
        keys=keys, expect_prompt_id="want-prompt", expect_regex="ab.*", timeout_ms=4000, poll_interval_ms=333
    )

    result = await _run(hub, _Req(None), request)

    assert hub.wait_for_guard_calls == [
        {
            "worker_id": WID,
            "expect_prompt_id": "want-prompt",
            "expect_regex": "ab.*",
            "timeout_ms": 4000,
            "poll_interval_ms": 333,
        }
    ]
    assert hub.send_owned_worker_calls == [
        {"worker_id": WID, "msg": {"type": "input", "data": keys, "ts": NOW}, "rest_hijack_id": HID}
    ]
    assert logger_recorder.info_calls == [
        ("rest_send_ok worker_id=%s hijack_id=%s client=%s keys_len=%d", WID, HID, "unknown", 130)
    ]
    assert hub.append_event_calls == [
        (
            WID,
            "hijack_send",
            {
                "hijack_id": HID,
                "keys": "k" * 120,
                "expect_prompt_id": "want-prompt",
                "expect_regex": "ab.*",
            },
        )
    ]
    assert hub.get_fresh_hijack_expiry_calls == [(WID, HID, 555.0)]
    assert result == {
        "ok": True,
        "worker_id": WID,
        "hijack_id": HID,
        "sent": keys,
        "matched_prompt_id": "matched-prompt",
        "lease_expires_at": 999.25,
    }
