#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill suite for ``app/ws_denial.py`` (wired into
``[tool.mutmut].pytest_add_cli_args_test_selection``).

NOT a coverage suite — ``test_ws_denial.py`` is that, and stays unwired. Every
assertion here exists to kill a specific mutant, so each one pins an exact
value rather than a shape: the close code and reason, the response status, the
raw header bytes, the body, and whether the ContextVar was set. A mutant that
drops a keyword argument, swaps a comparison, or nulls a literal changes one of
those exactly.

Two properties this suite depends on:

* ``uvicorn.error`` is a process-global logger, so the install tests must own
  its filter list for the duration of a test — otherwise a filter left by an
  earlier app makes the "not any(...)" guard untestable in both directions.
* ``_denial_sent`` is a ContextVar; every set is reset via its token so a
  mutant's write cannot leak into the next test and mask a later assertion.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from starlette.websockets import WebSocket

from provide.uterm.server.app.ws_denial import (
    _UVICORN_INCOMPLETE_HANDSHAKE,
    _UVICORN_LOGGER,
    WebSocketAuthDenied,
    _denial_sent,
    _IncompleteHandshakeFilter,
    handle_ws_auth_denied,
    install_ws_denial_support,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_DETAIL = "authentication required"


class _Recorder:
    """Capture the raw ASGI messages the handler sends."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


def _websocket(*, extension: bool) -> tuple[WebSocket, _Recorder]:
    scope: dict[str, Any] = {"type": "websocket", "path": "/ws/browser/w1/term"}
    if extension:
        scope["extensions"] = {"websocket.http.response": {}}
    recorder = _Recorder()

    async def _receive() -> dict[str, Any]:  # pragma: no cover — nothing inbound on a refusal
        return {"type": "websocket.connect"}

    return WebSocket(scope, receive=_receive, send=recorder), recorder


async def _refuse(*, extension: bool, status_code: int = 401) -> _Recorder:
    websocket, recorder = _websocket(extension=extension)
    token = _denial_sent.set(False)
    try:
        await handle_ws_auth_denied(websocket, WebSocketAuthDenied(status_code, _DETAIL))
        assert _denial_sent.get() is extension, "the ContextVar must be set only when a denial was written"
    finally:
        _denial_sent.reset(token)
    return recorder


def _start(recorder: _Recorder) -> dict[str, Any]:
    assert recorder.messages[0]["type"] == "websocket.http.response.start"
    return recorder.messages[0]


def _body(recorder: _Recorder) -> Any:
    assert recorder.messages[1]["type"] == "websocket.http.response.body"
    return json.loads(recorder.messages[1]["body"])


class TestWebSocketAuthDenied:
    """__init__ assigns all three of its arguments."""

    def test_the_detail_reaches_the_base_exception(self) -> None:
        assert str(WebSocketAuthDenied(401, _DETAIL)) == _DETAIL

    def test_status_code_is_retained(self) -> None:
        assert WebSocketAuthDenied(403, _DETAIL).status_code == 403

    def test_detail_is_retained(self) -> None:
        assert WebSocketAuthDenied(401, _DETAIL).detail == _DETAIL


class TestFallbackClose:
    """No denial extension: close before accept, with an exact code and reason."""

    async def test_exactly_one_close_message_is_sent(self) -> None:
        recorder = await _refuse(extension=False)
        assert len(recorder.messages) == 1
        assert recorder.messages[0]["type"] == "websocket.close"

    async def test_the_close_code_is_the_policy_violation(self) -> None:
        recorder = await _refuse(extension=False)
        assert recorder.messages[0]["code"] == 1008

    async def test_the_close_reason_is_the_detail(self) -> None:
        recorder = await _refuse(extension=False)
        assert recorder.messages[0]["reason"] == _DETAIL


class TestDenialResponse:
    """The extension is advertised: answer with the status the caller asked for."""

    async def test_a_denial_response_is_sent_rather_than_a_close(self) -> None:
        recorder = await _refuse(extension=True)
        assert [message["type"] for message in recorder.messages] == [
            "websocket.http.response.start",
            "websocket.http.response.body",
        ]

    async def test_the_status_is_the_one_the_exception_carried(self) -> None:
        assert _start(await _refuse(extension=True, status_code=401))["status"] == 401
        assert _start(await _refuse(extension=True, status_code=403))["status"] == 403

    async def test_a_401_carries_the_bearer_challenge(self) -> None:
        headers = _start(await _refuse(extension=True, status_code=401))["headers"]
        assert (b"www-authenticate", b"Bearer") in headers

    async def test_a_403_carries_no_challenge(self) -> None:
        headers = _start(await _refuse(extension=True, status_code=403))["headers"]
        assert not any(name == b"www-authenticate" for name, _ in headers)

    async def test_the_body_is_the_detail_under_the_detail_key(self) -> None:
        assert _body(await _refuse(extension=True)) == {"detail": _DETAIL}


class TestExtensionLookup:
    """The scope lookup must use the right key, with the right default."""

    async def test_an_absent_extensions_key_takes_the_fallback(self) -> None:
        recorder = await _refuse(extension=False)
        assert recorder.messages[0]["type"] == "websocket.close"

    async def test_an_extensions_map_without_our_extension_takes_the_fallback(self) -> None:
        websocket, recorder = _websocket(extension=False)
        websocket.scope["extensions"] = {"some.other.extension": {}}
        token = _denial_sent.set(False)
        try:
            await handle_ws_auth_denied(websocket, WebSocketAuthDenied(401, _DETAIL))
        finally:
            _denial_sent.reset(token)
        assert recorder.messages[0]["type"] == "websocket.close"


class TestIncompleteHandshakeFilter:
    """The filter drops our own denial's record and nothing else."""

    def _record(self, message: str) -> logging.LogRecord:
        return logging.LogRecord(_UVICORN_LOGGER, logging.ERROR, __file__, 0, message, None, None)

    @pytest.mark.parametrize("denial_sent", [False, True])
    def test_an_unrelated_record_always_passes(self, denial_sent: bool) -> None:
        """Both states, so inverting the message comparison cannot survive."""
        token = _denial_sent.set(denial_sent)
        try:
            assert _IncompleteHandshakeFilter().filter(self._record("connection closed")) is True
        finally:
            _denial_sent.reset(token)

    def test_the_uvicorn_record_passes_when_we_did_not_deny(self) -> None:
        token = _denial_sent.set(False)
        try:
            assert _IncompleteHandshakeFilter().filter(self._record(_UVICORN_INCOMPLETE_HANDSHAKE)) is True
        finally:
            _denial_sent.reset(token)

    def test_the_uvicorn_record_is_dropped_when_we_denied(self) -> None:
        token = _denial_sent.set(True)
        try:
            assert _IncompleteHandshakeFilter().filter(self._record(_UVICORN_INCOMPLETE_HANDSHAKE)) is False
        finally:
            _denial_sent.reset(token)


@pytest.fixture
def owned_uvicorn_logger() -> Iterator[logging.Logger]:
    """Give the test exclusive control of the process-global uvicorn logger."""
    uvicorn_logger = logging.getLogger(_UVICORN_LOGGER)
    original = list(uvicorn_logger.filters)
    for existing in original:
        uvicorn_logger.removeFilter(existing)
    root_original = list(logging.getLogger().filters)
    try:
        yield uvicorn_logger
    finally:
        for leftover in list(uvicorn_logger.filters):
            uvicorn_logger.removeFilter(leftover)
        for existing in original:
            uvicorn_logger.addFilter(existing)
        # getLogger(None) is the ROOT logger, so a mutant that loses the logger
        # name installs there instead; leave it as we found it either way.
        root = logging.getLogger()
        for leftover in list(root.filters):
            if leftover not in root_original:
                root.removeFilter(leftover)


class TestInstallWsDenialSupport:
    def test_the_handler_is_registered_under_the_exception_type(self, owned_uvicorn_logger: logging.Logger) -> None:
        app = FastAPI()
        install_ws_denial_support(app)
        assert app.exception_handlers[WebSocketAuthDenied] is handle_ws_auth_denied

    def test_the_filter_lands_on_the_uvicorn_logger_specifically(self, owned_uvicorn_logger: logging.Logger) -> None:
        """Not the root logger, which is where a lost logger name would put it."""
        install_ws_denial_support(FastAPI())
        assert [type(f) for f in owned_uvicorn_logger.filters] == [_IncompleteHandshakeFilter]
        assert not any(isinstance(f, _IncompleteHandshakeFilter) for f in logging.getLogger().filters)

    def test_a_second_install_adds_no_second_filter(self, owned_uvicorn_logger: logging.Logger) -> None:
        install_ws_denial_support(FastAPI())
        install_ws_denial_support(FastAPI())
        assert len(owned_uvicorn_logger.filters) == 1

    def test_the_installed_filter_is_functional(self, owned_uvicorn_logger: logging.Logger) -> None:
        """A None placed in the filter list would satisfy a count but not this."""
        install_ws_denial_support(FastAPI())
        installed = owned_uvicorn_logger.filters[0]
        record = logging.LogRecord(
            _UVICORN_LOGGER, logging.ERROR, __file__, 0, _UVICORN_INCOMPLETE_HANDSHAKE, None, None
        )
        token = _denial_sent.set(True)
        try:
            assert installed.filter(record) is False
        finally:
            _denial_sent.reset(token)
