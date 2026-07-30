#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Replay the ANSI/emulator differential fuzz corpus against the reference.

This is the reference side of ``conformance/fuzz/ansi_emulator_fuzz.json``: it
proves the corpus still describes what CPython does, so a port failing a case is
failing against something true rather than against a stale recording.

It deliberately does **not** import the generator. Every value is read out of
the file the way a port with no Python must read it, so a format the ports
cannot consume fails here first.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from provide.uterm.ansi import normalize_colors, upgrade_to_256, upgrade_to_truecolor
from provide.uterm.emulator import TerminalEmulator

_CORPUS = Path(__file__).resolve().parents[4] / "conformance" / "fuzz" / "ansi_emulator_fuzz.json"
_SCHEMA = "provide-uterm/ansi-emulator-fuzz/1"

#: Case counts, pinned here as well as in the corpus. A replay that iterated
#: nothing would pass, so the count is asserted rather than trusted.
_EXPECTED_COUNTS = {
    "normalize": 112,
    "upgrade_256": 96,
    "upgrade_truecolor": 96,
    "emulator": 128,
    "regressions": 3,
}

_TRANSFORMS = {
    "normalize": normalize_colors,
    "upgrade_256": upgrade_to_256,
    "upgrade_truecolor": upgrade_to_truecolor,
}


def _decode(value: str) -> str:
    """A ``*_b64`` field, back to the text it carries."""
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(_CORPUS.read_text(encoding="utf-8"))
    # Refuse an unrecognised version outright rather than skipping the cases it
    # does not understand: a silently-empty replay is the failure mode here.
    assert loaded["schema"] == _SCHEMA, f"unknown corpus schema {loaded['schema']!r}"
    return loaded


class TestTheCorpusItself:
    def test_every_family_is_the_size_it_declares(self, corpus: dict[str, Any]) -> None:
        for family, expected in _EXPECTED_COUNTS.items():
            assert corpus["counts"][family] == expected, family
            assert len(corpus[family]) == expected, family

    def test_every_case_id_is_unique(self, corpus: dict[str, Any]) -> None:
        ids = [case["id"] for family in _EXPECTED_COUNTS for case in corpus[family]]
        assert len(ids) == len(set(ids))

    def test_the_document_is_ascii(self) -> None:
        # The whole point of the base64 fields: no reader has to agree with
        # CPython about file encoding or which code points a JSON string may
        # carry raw.
        assert _CORPUS.read_text(encoding="utf-8").isascii()

    def test_the_geometry_is_recorded(self, corpus: dict[str, Any]) -> None:
        # A port replaying at a different size would disagree about wrapping for
        # reasons that have nothing to do with its parser.
        assert corpus["geometry"] == {"cols": 20, "rows": 6}


class TestPureTransforms:
    """``normalize`` / ``upgrade_256`` / ``upgrade_truecolor``.

    Nothing but the port's own code decides these answers, so a divergence is
    unambiguously a port bug rather than a disagreement about a dependency.
    """

    @pytest.mark.parametrize("family", list(_TRANSFORMS))
    def test_matches_the_reference(self, corpus: dict[str, Any], family: str) -> None:
        transform = _TRANSFORMS[family]
        asserted = 0
        for case in corpus[family]:
            assert transform(_decode(case["in_b64"])) == _decode(case["out_b64"]), case["id"]
            asserted += 1
        assert asserted == _EXPECTED_COUNTS[family]


class TestEmulator:
    """Generated streams through the emulator, driven both ways."""

    @staticmethod
    def _drive(chunks: list[str]) -> dict[str, Any]:
        emulator = TerminalEmulator(cols=20, rows=6)
        for chunk in chunks:
            emulator.process(chunk.encode("utf-8"))
        snapshot = emulator.get_snapshot()
        return {
            "screen_b64": base64.b64encode(str(snapshot["screen"]).encode("utf-8")).decode("ascii"),
            "cursor": {"x": snapshot["cursor"]["x"], "y": snapshot["cursor"]["y"]},
            "cols": snapshot["cols"],
            "rows": snapshot["rows"],
            "cursor_at_end": snapshot["cursor_at_end"],
            "has_trailing_space": snapshot["has_trailing_space"],
            "ansi_screen_b64": base64.b64encode(emulator.ansi_screen().encode("utf-8")).decode("ascii"),
        }

    @pytest.mark.parametrize("family", ["emulator", "regressions"])
    def test_both_drives_match_their_own_recording(self, corpus: dict[str, Any], family: str) -> None:
        asserted = 0
        for case in corpus[family]:
            chunks = [_decode(chunk) for chunk in case["chunks_b64"]]
            assert self._drive(chunks) == case["chunked"], f"{case['id']} (chunked)"
            assert self._drive(["".join(chunks)]) == case["single"], f"{case['id']} (single)"
            asserted += 1
        assert asserted == _EXPECTED_COUNTS[family]

    def test_the_two_drives_are_compared_separately(self, corpus: dict[str, Any]) -> None:
        """At least one case must distinguish them, or the second drive proves nothing.

        Only one generated case currently does, which is itself worth knowing:
        pyte holds a partial escape sequence across a feed properly, so where a
        chunk boundary falls rarely changes the screen. A port that buffered
        naively would fail that one case and nothing else would notice.
        """
        differing = [case["id"] for case in corpus["emulator"] if case["chunked"] != case["single"]]
        assert differing, "no case distinguishes the chunked and whole drives"

    def test_no_recorded_stream_raises(self, corpus: dict[str, Any]) -> None:
        """The invariant behind the whole corpus: arbitrary terminal output must
        never raise out of the emulator. Three separate crash classes were found
        while generating this file — surplus CSI parameters, a parameter value
        the handler does not implement, and a private-mode keyword — each of
        which killed the read loop that feeds a session's own output."""
        for case in corpus["emulator"]:
            emulator = TerminalEmulator(cols=20, rows=6)
            for chunk in case["chunks_b64"]:
                emulator.process(_decode(chunk).encode("utf-8"))
