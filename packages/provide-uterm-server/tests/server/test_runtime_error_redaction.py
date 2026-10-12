#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Runtime error text goes through the recording redactor.

``str(exc)`` can quote what a connector was handed — a URL with credentials, a
rejected command line. With ``recording.redact_sensitive`` on, the same
redactor that scrubs the recording is applied before the text is kept for
``status().last_error``, written to the server log, or recorded as a
``runtime_error`` event. With it off, the text is kept verbatim as before.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime

_SECRET = "S3cretValue"  # pragma: allowlist secret
_MESSAGE = f"connect failed: password={_SECRET}"


def _runtime(*, redact_sensitive: bool) -> HostedSessionRuntime:
    return HostedSessionRuntime(
        SessionDefinition(session_id="err-red", display_name="x", connector_type="shell", auto_start=False),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(redact_sensitive=redact_sensitive),
    )


async def _run_until_done(rt: HostedSessionRuntime) -> None:
    await rt.start()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if rt._task is not None and rt._task.done():
            return
    pytest.fail("runtime task did not finish")


def _logged_errors(log: MagicMock) -> list[str]:
    """Every positional argument passed to logger.warning/error, as text."""
    out: list[str] = []
    for method in (log.warning, log.error):
        for call in method.call_args_list:
            out.extend(str(arg) for arg in call.args)
    return out


async def _value_error_run(*, redact_sensitive: bool) -> tuple[HostedSessionRuntime, AsyncMock, MagicMock]:
    rt = _runtime(redact_sensitive=redact_sensitive)
    rec = AsyncMock()
    rt._logger = rec

    async def _bad_start() -> None:
        raise ValueError(_MESSAGE)

    with (
        patch.object(rt, "_start_connector", _bad_start),
        patch("provide.uterm.server.runtime.logger") as log,
    ):
        await _run_until_done(rt)
    return rt, rec, log


async def test_permanent_value_error_text_is_redacted_everywhere() -> None:
    rt, rec, log = await _value_error_run(redact_sensitive=True)

    assert rt._last_error == "connect failed: [PASSWORD_REDACTED]"
    assert rt.status().last_error == rt._last_error
    rec.log_event.assert_awaited_with("runtime_error", {"error": rt._last_error, "permanent": True})
    logged = _logged_errors(log)
    assert rt._last_error in logged
    assert all(_SECRET not in text for text in logged)


async def test_permanent_value_error_text_is_verbatim_with_redaction_off() -> None:
    rt, rec, log = await _value_error_run(redact_sensitive=False)

    assert rt._last_error == _MESSAGE
    rec.log_event.assert_awaited_with("runtime_error", {"error": _MESSAGE, "permanent": True})
    assert _MESSAGE in _logged_errors(log)


async def test_permanent_http_error_text_is_redacted() -> None:
    rt = _runtime(redact_sensitive=True)

    class FakeStatusError(Exception):
        status_code = 401

    class _FakeCtx:
        async def __aenter__(self) -> None:
            raise FakeStatusError(_MESSAGE)

        async def __aexit__(self, *_: object) -> None:
            return None

    connector = AsyncMock()
    connector.is_connected = MagicMock(return_value=True)
    fake_ws_mod = MagicMock()
    fake_ws_mod.connect = MagicMock(return_value=_FakeCtx())
    events: list[tuple[str, dict[str, Any]]] = []

    async def _capture(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    real_ws = sys.modules.pop("websockets", None)
    sys.modules["websockets"] = fake_ws_mod
    try:
        with (
            patch("provide.uterm.server.runtime.build_connector", return_value=connector),
            patch("provide.uterm.server.runtime.logger") as log,
            patch.object(rt, "_log_event", _capture),
        ):
            await _run_until_done(rt)
    finally:
        if real_ws is not None:
            sys.modules["websockets"] = real_ws
        else:
            sys.modules.pop("websockets", None)

    assert rt._last_error == "connect failed: [PASSWORD_REDACTED]"
    assert events == [("runtime_error", {"error": rt._last_error})]
    logged = _logged_errors(log)
    assert rt._last_error in logged
    assert all(_SECRET not in text for text in logged)


async def test_transient_error_text_is_redacted() -> None:
    rt = _runtime(redact_sensitive=True)
    events: list[tuple[str, dict[str, Any]]] = []

    async def _capture(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))
        rt._stop.set()  # one failed attempt is enough

    async def _bad_start() -> None:
        raise RuntimeError(_MESSAGE)

    with (
        patch.object(rt, "_start_connector", _bad_start),
        patch.object(rt, "_log_event", _capture),
        patch("provide.uterm.server.runtime.asyncio.sleep", AsyncMock()),
        patch("provide.uterm.server.runtime.logger") as log,
    ):
        await rt.start()
        assert rt._task is not None
        await asyncio.wait_for(rt._task, timeout=5)

    assert rt._last_error == "connect failed: [PASSWORD_REDACTED]"
    assert events == [("runtime_error", {"error": rt._last_error})]
    logged = _logged_errors(log)
    assert rt._last_error in logged
    assert all(_SECRET not in text for text in logged)


def test_the_recording_logger_gets_the_runtime_redactor() -> None:
    rt = _runtime(redact_sensitive=True)
    assert rt._redactor is not None
    assert rt._redactor(f"password={_SECRET}") == "[PASSWORD_REDACTED]"
    assert _runtime(redact_sensitive=False)._redactor is None
