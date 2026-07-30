#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Surplus CSI parameters must not crash the emulator.

Terminal output is untrusted: it is whatever the session happens to run. pyte
dispatches every parameter of a CSI sequence positionally into a handler with
fixed arity, so a sequence carrying one parameter more than its handler accepts
raises ``TypeError`` out of ``Stream.feed`` — and ``TerminalEmulator.process``
let it propagate. Its only caller is a transport read loop whose ``except``
clause covers cancellation and connection errors, so the exception killed the
task: six bytes of a program's own output stopped a session reading its output.

Real terminals ignore parameters they have no use for. Found by the ANSI fuzz
corpus generator on its first run, which crashed on ``ESC[1;2M``; a sweep of
every CSI final byte then found 62 crashing shapes across 21 of them.
"""

from __future__ import annotations

import string

import pytest

from provide.uterm.emulator import TerminalEmulator

#: Every CSI final byte whose pyte handler took fewer parameters than a sequence
#: can carry, found by sweeping the alphabet rather than by reading pyte.
_ARITY_SENSITIVE_FINALS = "acdefgnrABCDEFGKLMPX@"


@pytest.mark.parametrize("final", list(_ARITY_SENSITIVE_FINALS))
@pytest.mark.parametrize("params", ["1;2", "1;2;3", "1;2;3;4"])
def test_surplus_csi_parameters_do_not_raise(final: str, params: str) -> None:
    emulator = TerminalEmulator(cols=20, rows=6)

    emulator.process(f"\x1b[{params}{final}".encode())

    # Still usable afterwards, which is the part that matters: a guard that
    # swallowed the error but left the emulator wedged would be no better.
    emulator.process(b"after")
    assert "after" in emulator.get_snapshot()["screen"]


def test_no_csi_final_byte_crashes_the_emulator() -> None:
    """The sweep itself, so a pyte upgrade adding an arity cannot reintroduce this."""
    crashed = []
    for final in string.ascii_letters + "@`{|}~":
        for params in ("1;2", "1;2;3", "1;2;3;4", "1;2;3;4;5;6"):
            emulator = TerminalEmulator(cols=20, rows=6)
            try:
                emulator.process(f"\x1b[{params}{final}".encode())
            except Exception as exc:
                crashed.append(f"ESC[{params}{final} -> {type(exc).__name__}: {exc}")
    assert crashed == []


def test_a_surplus_parameter_is_ignored_rather_than_changing_the_effect() -> None:
    """Ignoring the extra parameter must leave the sequence doing its job.

    A shim that dropped *all* the parameters would also stop the emulator
    crashing, and would silently turn every parameterised sequence into its
    default. ``ESC[3B`` moves the cursor down three lines; ``ESC[3;9B`` must do
    the same rather than moving one.
    """
    with_surplus = TerminalEmulator(cols=20, rows=6)
    with_surplus.process(b"\x1b[3;9B")

    plain = TerminalEmulator(cols=20, rows=6)
    plain.process(b"\x1b[3B")

    assert with_surplus.get_snapshot()["cursor"] == plain.get_snapshot()["cursor"]
    assert with_surplus.get_snapshot()["cursor"]["y"] == 3


def test_the_parameters_a_handler_does_accept_are_still_honoured() -> None:
    """Truncation must keep the leading parameters, not just the first one.

    ``ESC[2;5H`` positions the cursor at row 2, column 5 — a two-parameter
    handler. A shim truncating to one argument would put it at row 2, column 0.
    """
    emulator = TerminalEmulator(cols=20, rows=6)
    emulator.process(b"\x1b[2;5H")

    cursor = emulator.get_snapshot()["cursor"]
    assert (cursor["y"], cursor["x"]) == (1, 4), "CSI H is 1-indexed on the wire"


def test_a_stream_continues_past_a_surplus_parameter_sequence() -> None:
    """The whole chunk is processed, not abandoned at the bad sequence.

    This is why the fix tolerates the surplus parameter rather than catching the
    error around ``feed``: catching it would lose everything after the offending
    sequence in that chunk, which is where the session's actual output is.
    """
    emulator = TerminalEmulator(cols=20, rows=6)

    # `ESC[1;2c` is a device-attributes report: it answers on the response
    # channel and leaves the screen alone, so both halves of the text must
    # survive. A screen-altering sequence would be the wrong probe here —
    # `ESC[1;2M` truncates to `delete_lines(1)`, which removes the line the
    # first half was written on, and that is the sequence doing its job.
    emulator.process(b"before\x1b[1;2cafter")

    screen = emulator.get_snapshot()["screen"]
    assert "before" in screen
    assert "after" in screen
