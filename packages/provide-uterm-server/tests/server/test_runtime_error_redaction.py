#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Runtime error text is redacted once per sink, under that sink's policy.

``str(exc)`` can quote what a connector was handed — a URL with credentials, a
rejected command line. ``status().last_error`` and the server log are not
recording sinks, so they always get the server-default ruleset, whatever
``recording.redact_sensitive`` says. The ``runtime_error`` event carries the
raw text to ``SessionLogger.log_event``, which redacts it per the recording
setting -- one redaction on that path, not two.
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


@pytest.mark.parametrize("redact_sensitive", [True, False])
async def test_status_and_log_are_redacted_whatever_the_recording_setting(redact_sensitive: bool) -> None:
    rt, rec, log = await _value_error_run(redact_sensitive=redact_sensitive)

    assert rt._last_error == "connect failed: [PASSWORD_REDACTED]"
    assert rt.status().last_error == rt._last_error
    logged = _logged_errors(log)
    assert rt._last_error in logged
    assert all(_SECRET not in text for text in logged)
    # The recorder gets the raw text and applies the recording policy itself.
    rec.log_event.assert_awaited_with("runtime_error", {"error": _MESSAGE, "permanent": True})


@pytest.mark.parametrize(
    ("redact_sensitive", "expected"), [(True, "connect failed: [PASSWORD_REDACTED]"), (False, _MESSAGE)]
)
async def test_recorded_runtime_error_follows_the_recording_setting(redact_sensitive: bool, expected: str) -> None:
    """End to end through a real SessionLogger: redacted exactly once, per recording.redact_sensitive."""
    from provide.uterm.recording import InMemoryRecordingStore
    from provide.uterm.session_logger import SessionLogger

    rt = _runtime(redact_sensitive=redact_sensitive)
    store = InMemoryRecordingStore()
    calls: list[str] = []
    base = rt._redactor

    def counting(text: str) -> str:
        calls.append(text)
        return base(text) if base is not None else text

    rt._logger = SessionLogger(store, redactor=counting if base is not None else None, flush_interval_s=3600)
    await rt._logger.start("err-red")

    async def _bad_start() -> None:
        raise ValueError(_MESSAGE)

    with patch.object(rt, "_start_connector", _bad_start), patch.object(rt, "_stop_recording", AsyncMock()):
        await _run_until_done(rt)
    await rt._logger.stop()

    recorded = [e["data"] for e in await store.get_entries("err-red", limit=50) if e["event"] == "runtime_error"]
    assert recorded == [{"error": expected, "permanent": True}]
    if base is not None:
        assert calls.count(_MESSAGE) == 1  # the raw text reached the recorder once, unredacted
        assert all("[PASSWORD_REDACTED]" not in c for c in calls)  # nothing was redacted twice


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
    assert events == [("runtime_error", {"error": _MESSAGE})]
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
    assert events == [("runtime_error", {"error": _MESSAGE})]
    logged = _logged_errors(log)
    assert rt._last_error in logged
    assert all(_SECRET not in text for text in logged)


def test_the_recording_logger_gets_the_runtime_redactor() -> None:
    rt = _runtime(redact_sensitive=True)
    assert rt._redactor is not None
    assert rt._redactor(f"password={_SECRET}") == "[PASSWORD_REDACTED]"
    assert _runtime(redact_sensitive=False)._redactor is None
    # The status/log redactor exists either way.
    assert _runtime(redact_sensitive=False)._error_redactor(f"password={_SECRET}") == "[PASSWORD_REDACTED]"
