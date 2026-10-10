#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Read-path annotation: one record per match, and escapes split across chunks.

Read-path rules run over every streamed ``term`` chunk AND every snapshot
screen. The stream records every match it sees; the snapshot path skips any
match already recorded (by the stream or an earlier snapshot), so the same
output is not annotated twice. And an escape sequence cut off at a chunk
boundary is carried into the next chunk before stripping, so its tail does not
glue itself onto the text that follows (``\\x1b[1`` + ``msudo`` -> ``msudo``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from provide.uterm.annotation import Annotation, PatternDetector
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime
from provide.uterm.server.runtime_helpers import (
    _MAX_ESCAPE_CARRY,
    _MAX_READ_ANNOTATION_KEYS,
    _read_annotation_key,
    _split_incomplete_escape,
)

_SUDO = "sudo command detected: sudo"
_RM = "Recursive force-remove detected: rm -rf"


def _runtime() -> tuple[HostedSessionRuntime, AsyncMock]:
    runtime = HostedSessionRuntime(
        SessionDefinition(session_id="cap-1", display_name="Capture", connector_type="shell", auto_start=False),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
        detector=PatternDetector(),
    )
    logger = AsyncMock()
    runtime._logger = logger
    return runtime, logger


def _annotations(logger: AsyncMock) -> list[str]:
    return [call.args[1]["description"] for call in logger.log_event.await_args_list if call.args[0] == "annotation"]


async def _term(runtime: HostedSessionRuntime, data: str) -> None:
    await runtime._send_outbound_frame(AsyncMock(), {"type": "term", "data": data})


async def _snapshot(runtime: HostedSessionRuntime, screen: str) -> None:
    await runtime._send_outbound_frame(AsyncMock(), {"type": "snapshot", "screen": screen})


# ---------------------------------------------------------------------------
# Finding 9: escape sequences split across chunks
# ---------------------------------------------------------------------------


async def test_an_escape_split_across_chunks_does_not_hide_a_match() -> None:
    runtime, logger = _runtime()

    await _term(runtime, "$ \x1b[1")
    assert runtime._escape_carry == "\x1b[1"
    await _term(runtime, "msudo rm -rf /\r\n")

    assert sorted(_annotations(logger)) == sorted([_SUDO, _RM])
    assert runtime._escape_carry == ""


async def test_a_bare_esc_at_the_end_of_a_chunk_is_carried() -> None:
    runtime, logger = _runtime()

    await _term(runtime, "$ \x1b")
    await _term(runtime, "[1msudo ls\r\n")

    assert _annotations(logger) == [_SUDO]


async def test_an_overlong_unterminated_sequence_is_released() -> None:
    # Not a real sequence: it is not held back for ever.
    runtime, _ = _runtime()
    junk = "\x1b[" + "1;" * _MAX_ESCAPE_CARRY

    await _term(runtime, junk)

    assert runtime._escape_carry == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ("", "")),
        ("plain", ("plain", "")),
        ("a\x1b", ("a", "\x1b")),
        ("a\x1b[", ("a", "\x1b[")),
        ("a\x1b[38;2;255", ("a", "\x1b[38;2;255")),
        ("a\x1b[1 ", ("a", "\x1b[1 ")),  # intermediate byte, still no final
        ("a\x1b[1mb", ("a\x1b[1mb", "")),  # complete
        ("a\x1b[1m", ("a\x1b[1m", "")),  # complete at the very end
        ("a\x1bc", ("a\x1bc", "")),  # complete two-byte sequence
        ("\x1b[1m\x1b[3", ("\x1b[1m", "\x1b[3")),  # only the last ESC counts
        ("\x1b[" + "1" * (_MAX_ESCAPE_CARRY - 2), ("", "\x1b[" + "1" * (_MAX_ESCAPE_CARRY - 2))),
        ("\x1b[" + "1" * (_MAX_ESCAPE_CARRY - 1), ("\x1b[" + "1" * (_MAX_ESCAPE_CARRY - 1), "")),
    ],
)
def test_split_incomplete_escape(text: str, expected: tuple[str, str]) -> None:
    assert _split_incomplete_escape(text) == expected


def test_the_carry_limit_is_64() -> None:
    assert _MAX_ESCAPE_CARRY == 64


# ---------------------------------------------------------------------------
# Finding 8: stream + snapshot dedupe
# ---------------------------------------------------------------------------


async def test_a_snapshot_after_the_stream_does_not_repeat_its_match() -> None:
    runtime, logger = _runtime()

    await _term(runtime, "$ sudo ls\r\n")
    await _snapshot(runtime, "$ sudo ls\n")

    assert _annotations(logger) == [_SUDO]


async def test_repeated_identical_snapshots_annotate_once() -> None:
    runtime, logger = _runtime()

    for _ in range(3):
        await _snapshot(runtime, "$ rm -rf build\n")

    assert _annotations(logger) == [_RM]


async def test_a_command_streamed_twice_is_annotated_twice() -> None:
    runtime, logger = _runtime()

    await _term(runtime, "$ sudo ls\r\n")
    await _term(runtime, "$ sudo ls\r\n")
    await _snapshot(runtime, "$ sudo ls\n$ sudo ls\n")

    assert _annotations(logger) == [_SUDO, _SUDO]


async def test_a_snapshot_still_records_what_the_stream_never_saw() -> None:
    runtime, logger = _runtime()

    await _term(runtime, "$ sudo ls\r\n")
    await _snapshot(runtime, "$ sudo ls\n$ rm -rf build\n")

    assert _annotations(logger) == [_SUDO, _RM]


def test_the_key_is_the_rule_label_and_its_described_match() -> None:
    annotation = Annotation(
        label="privilege_escalation",
        description="sudo command detected: sudo",
        severity="high",
        source="detector",
        principal="system",
    )
    assert _read_annotation_key(annotation) == "privilege_escalation\x00sudo command detected: sudo"


def test_the_key_set_is_bounded_and_starts_over_when_full() -> None:
    runtime, _ = _runtime()
    assert _MAX_READ_ANNOTATION_KEYS == 1024
    for i in range(_MAX_READ_ANNOTATION_KEYS):
        runtime._remember_read_annotation(f"k{i}")
    assert len(runtime._read_annotation_keys) == _MAX_READ_ANNOTATION_KEYS

    runtime._remember_read_annotation("next")

    assert runtime._read_annotation_keys == {"next"}


async def test_a_new_recording_starts_with_no_keys_and_no_carry() -> None:
    runtime, logger = _runtime()
    await _term(runtime, "$ sudo ls\r\n\x1b[1")
    assert runtime._read_annotation_keys
    assert runtime._escape_carry

    await runtime._stop_recording()

    logger.stop.assert_awaited_once()
    assert runtime._read_annotation_keys == set()
    assert runtime._escape_carry == ""
