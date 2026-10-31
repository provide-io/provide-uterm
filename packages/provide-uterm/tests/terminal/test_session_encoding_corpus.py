#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The session-decoding corpus still describes this emulator.

``conformance/snapshot/session_encoding.json`` is what every port's emulator
must hold after the same reads in the same encoding; this is the reference side
of it, read the way a port with no Python reads it.
"""

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from provide.uterm.emulator import TerminalEmulator

_CORPUS = Path(__file__).resolve().parents[4] / "conformance" / "snapshot" / "session_encoding.json"
_RECORDS: list[dict[str, Any]] = json.loads(_CORPUS.read_text(encoding="utf-8"))["records"]


def test_the_corpus_covers_both_encodings() -> None:
    assert {record["encoding"] for record in _RECORDS} == {"utf-8", "cp437"}
    assert len(_RECORDS) >= 10


@pytest.mark.parametrize("record", _RECORDS, ids=[record["name"] for record in _RECORDS])
def test_the_emulator_holds_what_the_corpus_records(record: dict[str, Any]) -> None:
    emulator = TerminalEmulator(record["cols"], record["rows"], receive_encoding=record["encoding"])
    for read in record["reads"]:
        emulator.process(base64.b64decode(read))
    snapshot = emulator.get_snapshot()

    assert (snapshot["screen"], snapshot["cursor"], emulator.ansi_screen()) == (
        record["screen"],
        record["cursor"],
        record["ansi_screen"],
    )
