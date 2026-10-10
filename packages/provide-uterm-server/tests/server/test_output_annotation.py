#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Read-path annotation on streamed terminal output, not only on snapshots.

A capture connector's snapshot is the raw tail of the output stream, and a
blinking cursor can fill that tail with redraws of one line, pushing out the
text a rule should have matched. Scanning the stream as it passes is what sees
it regardless of what the snapshot happens to keep.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from provide.uterm.annotation import PatternDetector
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime

_KEY = "AKIA0123456789AB"  # pragma: allowlist secret


def _runtime(*, logged: bool = True) -> tuple[HostedSessionRuntime, AsyncMock]:
    runtime = HostedSessionRuntime(
        SessionDefinition(session_id="cap-1", display_name="Capture", connector_type="shell", auto_start=False),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
        detector=PatternDetector(),
    )
    logger = AsyncMock()
    runtime._logger = logger if logged else None
    return runtime, logger


def _annotations(logger: AsyncMock) -> list[str]:
    return [call.args[1]["description"] for call in logger.log_event.await_args_list if call.args[0] == "annotation"]


async def test_streamed_output_is_scanned_through_its_escape_sequences() -> None:
    runtime, logger = _runtime()
    styled = f"\x1b[12;5H\x1b[38;2;255;176;0m{_KEY}\x1b[0m"

    await runtime._send_outbound_frame(AsyncMock(), {"type": "term", "data": styled})

    assert _annotations(logger) == ["AWS access key detected in read"]


async def test_a_match_split_across_frames_is_found_once() -> None:
    runtime, logger = _runtime()

    await runtime._send_outbound_frame(AsyncMock(), {"type": "term", "data": "DROP TA"})
    await runtime._send_outbound_frame(AsyncMock(), {"type": "term", "data": "BLE callers;"})

    assert _annotations(logger) == ["SQL DROP statement detected: DROP TABLE"]


async def test_nothing_is_scanned_when_nothing_is_recorded() -> None:
    runtime, _ = _runtime(logged=False)
    ws = AsyncMock()

    await runtime._send_outbound_frame(ws, {"type": "term", "data": _KEY})

    ws.send.assert_awaited_once()


async def test_a_rendered_snapshot_is_read_through_its_colours() -> None:
    # A capture connector's snapshot is the emulator's rendered screen: every
    # row carries SGR codes and ends in a reset. Both the password-prompt check
    # (which looks for a colon at the end of the screen) and read-path rules
    # must see the text, not the codes.
    runtime, logger = _runtime()
    styled_rows = "\x1b[1mDROP \x1b[0mTABLE callers;\x1b[0m\n\x1b[33mPassword: \x1b[0m"

    await runtime._log_snapshot({"type": "snapshot", "screen": styled_rows})

    assert "SQL DROP statement detected: DROP TABLE" in _annotations(logger)
    assert runtime._at_password_prompt is True
