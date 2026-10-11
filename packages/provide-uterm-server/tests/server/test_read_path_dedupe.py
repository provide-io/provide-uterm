#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Read-path annotation: one record per match, and escapes split across chunks.

Read-path rules run over every streamed ``term`` chunk AND every snapshot
screen. The snapshot path skips any match already recorded (by the stream or an
earlier snapshot); the stream records every match except one a snapshot recorded
just before it, in the very next chunk, which it consumes once (pending snapshot
keys expire with that chunk). So the same output is annotated once whichever
path sees it first, a command run twice is annotated twice, and matches are
keyed by a digest of (rule id, matched text), so different credentials of one
rule are each recorded. And an escape sequence cut off at a chunk
boundary is carried into the next chunk before stripping, so its tail does not
glue itself onto the text that follows (``\\x1b[1`` + ``msudo`` -> ``msudo``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from provide.uterm import MAX_ESCAPE_CARRY, split_incomplete_escape
from provide.uterm.annotation import Annotation, PatternDetector, annotation_match_key
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime
from provide.uterm.server.runtime_helpers import (
    _MAX_READ_ANNOTATION_KEYS,
    _add_bounded,
    _read_annotation_key,
)

_SUDO = "sudo command detected: sudo"
_RM = "Recursive force-remove detected: rm -rf"
_AWS = "AWS access key detected in read"


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
    junk = "\x1b[" + "1;" * MAX_ESCAPE_CARRY

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
        ("\x1b[" + "1" * (MAX_ESCAPE_CARRY - 2), ("", "\x1b[" + "1" * (MAX_ESCAPE_CARRY - 2))),
        ("\x1b[" + "1" * (MAX_ESCAPE_CARRY - 1), ("\x1b[" + "1" * (MAX_ESCAPE_CARRY - 1), "")),
    ],
)
def test_split_incomplete_escape(text: str, expected: tuple[str, str]) -> None:
    assert split_incomplete_escape(text) == expected


def test_the_carry_limit_is_64() -> None:
    assert MAX_ESCAPE_CARRY == 64


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


async def test_a_stream_match_a_snapshot_already_recorded_is_not_recorded_again() -> None:
    # Snapshot first, then the same text on the stream: one record. The stream
    # consumes the snapshot's record once; a later repeat is a new occurrence.
    runtime, logger = _runtime()

    await _snapshot(runtime, "$ sudo ls\n")
    assert runtime._snapshot_only_keys
    await _term(runtime, "$ sudo ls\r\n")
    assert runtime._snapshot_only_keys == set()
    assert _annotations(logger) == [_SUDO]

    await _term(runtime, "$ sudo ls\r\n")
    await _snapshot(runtime, "$ sudo ls\n$ sudo ls\n")
    assert _annotations(logger) == [_SUDO, _SUDO]


async def test_a_snapshot_then_its_chunk_records_once_and_a_later_new_match_records_again() -> None:
    runtime, logger = _runtime()

    await _snapshot(runtime, "$ sudo ls\n")
    await _term(runtime, "$ sudo ls\r\n")  # the chunk carrying the snapshot's text
    assert _annotations(logger) == [_SUDO]

    await _term(runtime, "$ sudo reboot\r\n")  # a genuinely new sudo
    assert _annotations(logger) == [_SUDO, _SUDO, "reboot command detected: reboot"]


async def test_pending_snapshot_keys_expire_with_the_next_chunk_even_without_a_match() -> None:
    # Missing an annotation is worse than an occasional duplicate: the pending key
    # dedupes only the chunk right after the snapshot.
    runtime, logger = _runtime()

    await _snapshot(runtime, "$ sudo ls\n")
    await _term(runtime, "unrelated output\r\n")
    assert runtime._snapshot_only_keys == set()
    await _term(runtime, "$ sudo ls\r\n")

    assert _annotations(logger) == [_SUDO, _SUDO]


async def test_a_pending_key_is_consumed_only_by_its_own_match() -> None:
    runtime, logger = _runtime()

    await _snapshot(runtime, "$ sudo ls\n")
    await _term(runtime, "$ rm -rf build\r\n")

    assert _annotations(logger) == [_SUDO, _RM]
    assert runtime._snapshot_only_keys == set()


async def test_an_empty_chunk_leaves_pending_keys_alone() -> None:
    runtime, logger = _runtime()

    await _snapshot(runtime, "$ sudo ls\n")
    await _term(runtime, "")
    await _term(runtime, "$ sudo ls\r\n")

    assert _annotations(logger) == [_SUDO]


async def test_different_credentials_of_one_rule_in_successive_snapshots_are_all_recorded() -> None:
    # Credential rules never embed the match in the description, so a key of
    # label+description would have dropped every AWS key after the first.
    runtime, logger = _runtime()

    await _snapshot(runtime, "AKIAIOSFODNN7EXAMPLE first\n")
    await _snapshot(runtime, "AKIAJ2345678ABCDWXYZ second\n")
    await _snapshot(runtime, "AKIAJ2345678ABCDWXYZ second\n")

    assert _annotations(logger) == [_AWS, _AWS]


async def test_different_credentials_on_the_stream_after_a_snapshot_are_recorded() -> None:
    runtime, logger = _runtime()

    await _snapshot(runtime, "AKIAIOSFODNN7EXAMPLE\n")
    await _term(runtime, "AKIAJ2345678ABCDWXYZ\r\n")

    assert _annotations(logger) == [_AWS, _AWS]


def test_the_key_is_the_detector_match_key_and_holds_no_raw_text() -> None:
    (annotation,) = PatternDetector().detect("read", "AKIAIOSFODNN7EXAMPLE", seq=1)
    key = _read_annotation_key(annotation)
    assert key == annotation_match_key("cred.aws_access_key", "AKIAIOSFODNN7EXA")
    assert "AKIA" not in key
    assert len(key) == 64


async def test_the_key_sets_hold_digests_not_secrets() -> None:
    runtime, _ = _runtime()

    await _snapshot(runtime, "AKIAIOSFODNN7EXAMPLE\n")
    await _term(runtime, "AKIAJ2345678ABCDWXYZ\r\n")

    for key in runtime._read_annotation_keys | runtime._snapshot_only_keys:
        assert "AKIA" not in key


def test_the_snapshot_only_set_is_bounded_too() -> None:
    keys: set[str] = set()
    for i in range(_MAX_READ_ANNOTATION_KEYS):
        _add_bounded(keys, f"k{i}")
    _add_bounded(keys, "next")
    assert keys == {"next"}


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
    await _snapshot(runtime, "$ rm -rf build\n")
    assert runtime._read_annotation_keys
    assert runtime._snapshot_only_keys
    assert runtime._escape_carry

    await runtime._stop_recording()

    logger.stop.assert_awaited_once()
    assert runtime._read_annotation_keys == set()
    assert runtime._snapshot_only_keys == set()
    assert runtime._escape_carry == ""


# ---------------------------------------------------------------------------
# An empty match key (an annotation the detector did not build) is never deduped
# ---------------------------------------------------------------------------


class _StubDetector:
    """Returns one annotation with an EMPTY match key for every read scan."""

    def detect(self, _event_type: str, _text: str, seq: int) -> list[Annotation]:
        return [
            Annotation(
                label="custom",
                description=f"custom #{seq}",
                severity="low",
                source="plugin",
                principal="system",
            )
        ]


async def test_an_empty_key_is_recorded_every_time_and_remembered_nowhere() -> None:
    runtime, logger = _runtime()
    stub = _StubDetector()
    runtime._detector = stub  # type: ignore[assignment]
    runtime._read_stream = stub  # type: ignore[assignment]

    await _snapshot(runtime, "a\n")
    await _snapshot(runtime, "a\n")
    await _term(runtime, "a\r\n")
    await _term(runtime, "a\r\n")

    assert len(_annotations(logger)) == 4
    assert runtime._read_annotation_keys == set()
    assert runtime._snapshot_only_keys == set()


def test_the_key_of_a_non_detector_annotation_is_empty() -> None:
    annotation = Annotation(label="x", description="y", severity="low", source="user", principal="tim")
    assert _read_annotation_key(annotation) == ""
