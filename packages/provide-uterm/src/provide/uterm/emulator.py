#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Terminal emulation via pyte.

Requires the ``emulator`` extra::

    pip install 'provide-uterm[emulator]'
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

try:
    import pyte
except ImportError as _e:  # pragma: no cover
    raise ImportError("pyte is required for TerminalEmulator: pip install 'provide-uterm[emulator]'") from _e

_CP437 = "cp437"

# Bounded rolling raw-output tail (decoded chars, ANSI/control intact). pyte's
# Screen keeps only the 25-row viewport, so output that scrolls off within a
# single server turn (e.g. a TWGS scan that scans-and-redisplays the prompt)
# is otherwise unrecoverable. This tail lets consumers recover that content.
# Kept small so it rides every snapshot frame cheaply.
_RAW_TAIL_MAX = 4096

#: ``CO_VARARGS`` from CPython's code-flag set: the function declares ``*args``.
_CO_VARARGS = 0x04


def _tolerant_of_surplus_params(method: Any) -> Any:
    """Wrap a pyte CSI handler so surplus parameters are dropped, not fatal.

    pyte hands every parameter of a CSI sequence to its handler positionally
    (``csi_dispatch[char](*params)``), and its handlers take fixed arities. So a
    sequence carrying one parameter more than its handler accepts raises
    ``TypeError`` out of ``Stream.feed``. Real terminals ignore parameters they
    have no use for, and terminal output is untrusted — it is whatever the
    session happens to run — so a crash is not an available answer.

    Truncating to the handler's own arity, rather than dropping the parameters
    altogether, is what keeps the sequence doing its job: ``ESC[3;9B`` still
    moves down three lines, and ``ESC[2;5H`` still reaches row two column five.
    """
    # A handler declaring ``*args`` cannot overflow on *count*, so it is never
    # truncated — and truncating it would be destructive, not merely useless.
    # ``select_graphic_rendition(self, *attrs)`` has a ``co_argcount`` of one, so
    # the limit below computes to zero: wrapping it truncated away every
    # attribute, turning each SGR sequence into a plain reset and silently
    # draining all colour from the rendered screen. The recorded emulator corpus
    # caught exactly that. Such a handler is still guarded against *raising*,
    # which is a separate failure from arity.
    varargs = bool(method.__code__.co_flags & _CO_VARARGS)
    limit = None if varargs else method.__code__.co_argcount - 1  # minus `self`

    def tolerant(self: Any, *params: Any, **options: Any) -> Any:
        # ``**options`` is not decoration: pyte dispatches a private-mode
        # sequence as ``handler(*params, private=True)``, so a wrapper taking
        # positional arguments only turns ``ESC[?25h`` — hide the cursor, which
        # any full-screen program sends — into a TypeError. The wrapper would
        # have introduced a crash of exactly the kind it exists to prevent.
        try:
            if limit is None:
                return method(self, *params, **options)
            return method(self, *params[:limit], **options)
        except Exception:
            # A handler that raises is treated as a sequence this terminal does
            # not implement, which is what a real terminal does with one. pyte
            # reaches here in at least two ways beyond arity: an erase handler
            # selects its interval by parameter value and leaves the local
            # unbound when the value is outside the set it implements, so
            # ``ESC[3K`` — four bytes — raised ``UnboundLocalError``.
            #
            # Swallowing is bounded to pyte's own dispatch, on input that is
            # untrusted by definition: terminal output is whatever the session
            # runs. The alternative is letting it reach a read loop that catches
            # only cancellation and connection errors, which killed the task.
            return None

    tolerant.__name__ = method.__name__
    tolerant.__qualname__ = method.__qualname__
    tolerant.__doc__ = method.__doc__
    return tolerant


class _TolerantScreen(pyte.Screen):
    """A :class:`pyte.Screen` that survives a CSI sequence with too many parameters.

    Every handler reachable from pyte's CSI dispatch table is wrapped, rather
    than the handful a sweep happened to find, so a pyte upgrade that tightens
    another arity cannot reintroduce the crash. Sixty-two crashing shapes across
    twenty-one final bytes existed before this: `ESC[1;2M`, `ESC[1;2A`,
    `ESC[1;2;3H` and so on, each six or seven bytes long.
    """


for _name in (
    "cursor_up",
    "cursor_down",
    "cursor_forward",
    "cursor_back",
    "cursor_down1",
    "cursor_up1",
    "cursor_to_column",
    "cursor_to_line",
    "cursor_position",
    "erase_in_display",
    "erase_in_line",
    "insert_lines",
    "delete_lines",
    "insert_characters",
    "delete_characters",
    "erase_characters",
    "report_device_attributes",
    "report_device_status",
    "set_margins",
    "clear_tab_stop",
    "set_mode",
    "reset_mode",
    "select_graphic_rendition",
):
    _inherited = getattr(pyte.Screen, _name, None)
    if _inherited is not None and getattr(_inherited, "__code__", None) is not None:
        setattr(_TolerantScreen, _name, _tolerant_of_surplus_params(_inherited))
    # A name pyte does not define is not an error: the list is written against a
    # dispatch table that a version bump may reshape, and a missing handler
    # simply has nothing to guard.
del _name


def _parse_screen_text(screen: pyte.Screen) -> str:
    return "\n".join(screen.display)


class TerminalEmulator:
    """VT/ANSI terminal emulator backed by pyte.

    Args:
        cols: Terminal width in columns (default 80).
        rows: Terminal height in rows (default 25).
        term: Terminal type string (default ``"ANSI"``).
        receive_encoding: Codec used to decode incoming terminal bytes. The
            default remains CP437 for byte-oriented BBS compatibility.

    Memory / scrollback bounds:
        Backed by ``pyte.Screen``, which is **bounded**: only the visible
        viewport (``cols * rows`` cells) is retained. Scrolling overwrites
        rather than buffering. There is no off-screen scrollback in this
        layer — applications that need history must record the raw byte
        stream separately (see :mod:`provide.uterm.replay`). This means
        ``get_snapshot`` and ``ansi_screen`` always allocate O(cols*rows),
        independent of session age. ``resize`` is also O(cols*rows): pyte
        re-flows the buffer, clipping rows that no longer fit.
    """

    def __init__(
        self,
        cols: int = 80,
        rows: int = 25,
        term: str = "ANSI",
        receive_encoding: str = _CP437,
    ) -> None:
        self.cols = cols
        self.rows = rows
        self.term = term
        self.receive_encoding = receive_encoding
        self._screen = _TolerantScreen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._dirty = True
        self._last_snapshot: dict[str, Any] | None = None
        self._raw_tail: str = ""

    def process(self, data: bytes) -> None:
        """Decode and feed raw terminal bytes through the emulator.

        Args:
            data: Raw bytes from a transport or file.
        """
        text = data.decode(self.receive_encoding, errors="replace")
        self._stream.feed(text)
        # Retain a bounded tail of the raw decoded stream (ANSI/control intact)
        # so consumers can recover content that scrolled off pyte's viewport
        # within a single turn. See ``_RAW_TAIL_MAX``.
        if text:
            self._raw_tail = (self._raw_tail + text)[-_RAW_TAIL_MAX:]
        self._dirty = True

    def get_raw_tail(self) -> str:
        """Return the bounded rolling tail of raw decoded output (ANSI intact)."""
        return self._raw_tail

    def _is_cursor_at_end(self) -> bool:
        # ``len(line) - 2`` slack is a deliberate heuristic, not an off-by-one:
        # BBS-style prompts often leave 1-2 trailing spaces after the input
        # caret (e.g. ``> `` or ``> _``), and detection rules need to treat
        # those as "still at the prompt" rather than "user has typed". A
        # tighter check (``>= len(line)``) misclassified TradeWars 2002 and
        # Major BBS prompts as not-at-end during the 2026-04 detector
        # rewrite; widen-back to 2 chars restored the prior pass rate.
        cursor_x = self._screen.cursor.x
        cursor_y = self._screen.cursor.y
        lines = self._screen.display
        for row_idx in range(len(lines) - 1, -1, -1):
            line = lines[row_idx].rstrip()
            if line:
                if cursor_y == row_idx:
                    return bool(int(cursor_x) >= len(line) - 2)
                return bool(int(cursor_y) > row_idx)
        return True

    def get_snapshot(self) -> dict[str, Any]:
        """Return the current screen state.

        Returns a dict with:

        - ``screen``: Full screen text (newline-separated rows).
        - ``screen_hash``: SHA-256 of the screen text.
        - ``cursor``: ``{"x": int, "y": int}``.
        - ``cols``, ``rows``, ``term``.
        - ``cursor_at_end``: ``True`` if cursor is at or past the last content line.
        - ``has_trailing_space``: ``True`` if the screen ends with a space or colon.
        - ``captured_at``: Unix timestamp of this snapshot (always fresh).
        """
        if self._last_snapshot is None or self._dirty:
            screen_text = _parse_screen_text(self._screen)
            screen_hash = hashlib.sha256(screen_text.encode("utf-8")).hexdigest()
            self._last_snapshot = {
                "screen": screen_text,
                "screen_hash": screen_hash,
                "cursor": {"x": self._screen.cursor.x, "y": self._screen.cursor.y},
                "cols": self.cols,
                "rows": self.rows,
                "term": self.term,
                "cursor_at_end": self._is_cursor_at_end(),
                "has_trailing_space": screen_text.rstrip() != screen_text.rstrip(" :"),
                "raw_tail": self._raw_tail,
            }
            self._dirty = False

        snap = dict(self._last_snapshot)
        snap["cursor"] = dict(snap.get("cursor") or {"x": 0, "y": 0})
        snap["captured_at"] = time.time()
        return snap

    def ansi_screen(self) -> str:
        """Return the current screen as a single string with ANSI SGR codes.

        Walks pyte's per-cell style buffer and emits SGR escape sequences
        whenever the style changes between adjacent cells. Use this when
        a downstream consumer (xterm.js dashboard, AnsiBuffer in a spy)
        needs the *visual* state including colors — :meth:`get_snapshot`'s
        plain ``screen`` field discards pyte's style attributes.

        Rows are joined with ``\\n``; each row ends with ``\\x1b[0m`` so a
        consumer's subsequent writes start from a clean attribute state.
        """
        # Local import keeps the render module out of the import-time
        # graph for callers that only need plain-text snapshots.
        from provide.uterm.render.buffer import render_cell_rows

        return "\n".join(render_cell_rows(self._screen.buffer, self.cols, self.rows))

    def reset(self) -> None:
        """Reset terminal to its initial state."""
        self._screen.reset()
        self._dirty = True

    def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal.

        Args:
            cols: New width in columns.
            rows: New height in rows.
        """
        self.cols = cols
        self.rows = rows
        # pyte's Screen.resize signature is (lines, columns) == (rows, cols),
        # unlike the constructor pyte.Screen(columns, lines). See
        # render/buffer.py:103 for the correct reference.
        self._screen.resize(rows, cols)
        self._dirty = True
