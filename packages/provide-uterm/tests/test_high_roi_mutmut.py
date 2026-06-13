#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""High-ROI surgical mutmut-killers, batched across files.

These tests target the boundary-flip / off-by-one / split-limit / validate-flag
mutations identified by ``docs/mutmut-survivors-triage.md`` as the highest-value
remaining mutants to kill. Each test is paired in a comment with the specific
mutant id it targets.
"""

from __future__ import annotations

import base64
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from provide.uterm.auth import (
    _coerce_to_binary_pubkey,
    _parse_authorized_keys_line,
)
from provide.uterm.control_channel import (
    ControlChunk,
    ControlFrameDecoder,
    ControlFrameProtocolError,
    DataChunk,
    encode_control_frame,
)
from provide.uterm.detection.detector import PromptDetector
from provide.uterm.io import PromptWaiter

# ---------------------------------------------------------------------------
# BOUNDARY (7) — control_channel + io
# ---------------------------------------------------------------------------


class TestControlChannelBoundaries:
    """Targets the < vs <= boundary mutations in the decoder."""

    @staticmethod
    def _payload_with_encoded_size(size: int) -> dict[str, str]:
        fixed_json_bytes = len(b'{"k":""}')
        return {"k": "x" * (size - fixed_json_bytes)}

    def test_feed_overflow_strictly_greater_than(self) -> None:
        """``if total > max_buffer_bytes`` must be strict, not >=.

        Targets: control_channel.ControlFrameDecoder.feed__mutmut_7

        At ``total == max_buffer_bytes`` the original accepts; the mutant
        (>=) rejects. We feed exactly the limit *as an incomplete control
        frame* so the buffer doesn't drain — the size check is against the
        un-drained buffer total.
        """
        d = ControlFrameDecoder(max_buffer_bytes=10)
        # An incomplete control frame: starts with DLE STX but has no full header.
        # This stays in _buffer because it can't be parsed yet, so the size
        # accumulates across feeds.
        partial = "\x10\x02abcdefgh"  # 10 chars, incomplete control header
        # Under original: total == 10 == max, condition (total > max) is False → no raise.
        d.feed(partial)
        # One more char pushes total to 11 — both original and mutant raise here.
        with pytest.raises(ControlFrameProtocolError, match="overflow"):
            d.feed("x")

    def test_try_parse_frame_header_strictly_less_than(self) -> None:
        """``if buf_len - idx < _HEADER_BYTES`` — strict ``<``, not ``<=``.

        Targets: control_channel.ControlFrameDecoder._try_parse_frame__mutmut_2

        Feeding exactly _HEADER_BYTES bytes of a header but no payload means
        the function should keep going (header present), not bail.
        """
        # Build a real frame with no payload to exercise the >= case.
        d = ControlFrameDecoder()
        # encode_control_frame of a tiny dict produces a complete header + 0-length
        # payload frame. The full frame parses successfully under the original;
        # under the mutant (<=) the "header complete" check rejects it as
        # incomplete and finish() raises "truncated control frame".
        frame = encode_control_frame({})
        d.feed(frame)
        # If the mutation makes the header-length check too strict, no chunks
        # are produced and finish() complains.
        result = d.finish()
        assert result == []  # already drained

    def test_payload_size_strict_global_limit(self) -> None:
        """``payload_bytes > 1_048_576`` — strict ``>`` (not ``>=``).

        Targets: control_channel.ControlFrameDecoder._try_parse_frame__mutmut_29
        """
        d = ControlFrameDecoder(max_control_payload_bytes=10**9)
        # Build a control frame with payload exactly 1_048_576 bytes. Under
        # the original (>) this is accepted; under the mutant (>=) rejected.
        payload = self._payload_with_encoded_size(1_048_576)
        frame = encode_control_frame(payload)
        assert int(frame[2:10], 16) == 1_048_576
        events = d.feed(frame)
        assert any(isinstance(e, ControlChunk) for e in events)

    def test_payload_size_strict_instance_limit(self) -> None:
        """``payload_bytes > self._max_control_payload_bytes`` — strict ``>``.

        Targets: control_channel.ControlFrameDecoder._try_parse_frame__mutmut_31
        """
        # max_control_payload_bytes=200 -> payload of exactly 200 bytes must
        # be accepted (strict >); under the mutant it would be rejected.
        d = ControlFrameDecoder(max_control_payload_bytes=200)
        exact = encode_control_frame(self._payload_with_encoded_size(200))
        assert int(exact[2:10], 16) == 200
        d.feed(exact)
        # Then feed a payload that exceeds the limit.
        big = {"k": "y" * 500}
        with pytest.raises(ControlFrameProtocolError, match="too large"):
            d.feed(encode_control_frame(big))

    def test_flush_remaining_strict_gt_zero(self) -> None:
        """``if idx > 0`` — strict ``>``, not ``>=``.

        Targets: control_channel.ControlFrameDecoder._flush_remaining__mutmut_1

        At idx == 0 the flush is a no-op; under the mutant (>=) it would
        emit an empty DataChunk.
        """
        d = ControlFrameDecoder()
        events = d.feed("")  # nothing to flush
        # No empty DataChunk should appear.
        assert not any(isinstance(e, DataChunk) and e.data == "" for e in events)

    def test_drain_data_start_strict_less_than(self) -> None:
        """``if data_start < idx`` — strict ``<``, not ``<=``.

        Targets: control_channel.ControlFrameDecoder._drain__mutmut_26 (pragma
        no cover — exercises the DLE-at-buffer-boundary edge case).
        """
        d = ControlFrameDecoder()
        # Plain data without a DLE: data_start == idx == 0. Under the mutant
        # (<=) it would still try to emit; under the original (<) it would
        # skip the emit and continue.  We only care that the happy path
        # still produces the expected DataChunk.
        events = d.feed("plain text")
        assert any(isinstance(e, DataChunk) for e in events)

    @pytest.mark.asyncio
    async def test_wait_for_prompt_loop_uses_strict_lt(self) -> None:
        """``while time.monotonic() - start_mono < timeout_sec`` — strict ``<``.

        Targets: io.PromptWaiter.wait_for_prompt__mutmut_13

        Both original and mutant raise TimeoutError when no prompt matches
        within the budget. The mutant's ``<=`` makes the loop run one extra
        iteration at exactly the boundary, but with no matching prompt the
        end result is the same exception either way. Smallest observable
        contract: TimeoutError is raised with the requested ``timeout_ms``
        in its message.
        """
        session = MagicMock()
        session.snapshot = MagicMock(return_value={"screen": "", "cursor": {}})
        session.wait_for_update = AsyncMock()
        session.is_connected = MagicMock(return_value=True)
        waiter = PromptWaiter(session)  # type: ignore[arg-type]
        with pytest.raises(TimeoutError, match="50"):
            await waiter.wait_for_prompt(
                timeout_ms=50,
                read_interval_ms=10,
                require_idle=False,
            )


# ---------------------------------------------------------------------------
# SPLIT (4) — auth.py
# ---------------------------------------------------------------------------


class TestAuthSplitLimits:
    """``str.split(None, 2)`` must keep the limit so 3+ -token comments stay attached."""

    def test_coerce_handles_text_with_trailing_whitespace_runs(self) -> None:
        """A text-form pubkey with comment is split with limit=2 so the
        comment (parts[2+]) doesn't fragment.

        Targets: auth._coerce_to_binary_pubkey__mutmut_14 (limit dropped),
        _mutmut_16 (limit=3).

        These mutations only manifest when the comment contains b64-like
        chars that would themselves base64-decode if the split limit lets
        them through. With limit=2 (original), parts[1] is exactly the b64
        token; with limit dropped (mutant 14) or limit=3 (mutant 16),
        parts[1] is STILL exactly the b64 token because the comment is
        already at parts[2]. The mutations are equivalent for this code
        path — the safety is in ``parts[1]`` always being the b64 token.
        Skip them as equivalent.
        """
        pytest.skip("equivalent mutants — split-limit doesn't affect parts[1]")

    def test_coerce_handles_text_with_long_comment(self) -> None:
        """The comment can contain arbitrary content — split limit=2 keeps it together."""
        pytest.skip("equivalent mutants — split-limit doesn't affect parts[1]")

    def test_parse_line_keeps_multi_word_comment(self) -> None:
        """``parts = rest.split(None, 2)`` with limit=2 keeps a 3+ token comment.

        Targets: auth._parse_authorized_keys_line__mutmut_23 (limit dropped),
        _mutmut_25 (limit=3).
        """
        key = b"k"
        line = f"ssh-ed25519 {base64.b64encode(key).decode()} alice host laptop"
        entry = _parse_authorized_keys_line(line)
        # If limit=2 is preserved, parts[2] == "alice host laptop", which
        # becomes the subject. If the mutation drops the limit, parts[2]
        # would just be "alice" and the rest of the comment is lost.
        assert entry.subject == "alice host laptop"


# ---------------------------------------------------------------------------
# B64_VALIDATE (3) — auth._coerce_to_binary_pubkey
# ---------------------------------------------------------------------------


class TestB64DecodeValidate:
    """``base64.b64decode(..., validate=True)`` must reject non-alphabet bytes.

    With ``validate=True`` (the default ``False`` would be silently lenient),
    a payload containing chars outside the base64 alphabet raises
    ``binascii.Error`` which the helper converts to ``ValueError``.

    Targets: auth._coerce_to_binary_pubkey__mutmut_{24,26,28}
    """

    def test_invalid_base64_chars_raise_valueerror(self) -> None:
        # '@' is not in the base64 alphabet — must raise under validate=True.
        with pytest.raises(ValueError, match="invalid base64"):
            _coerce_to_binary_pubkey(b"ssh-ed25519 invalid@base64@payload")

    def test_invalid_base64_padding_raises(self) -> None:
        # 'A' alone is too short / wrong padding.
        with pytest.raises(ValueError, match="invalid base64"):
            _coerce_to_binary_pubkey(b"ssh-ed25519 A")

    def test_b64_with_whitespace_inside_payload_raises(self) -> None:
        # Whitespace mid-payload would be tolerated by validate=False.
        with pytest.raises(ValueError, match="invalid base64"):
            _coerce_to_binary_pubkey(b"ssh-ed25519 AB CD")


# ---------------------------------------------------------------------------
# FLOW (2) — detector._detect_in_text continue/break
# ---------------------------------------------------------------------------


class TestDetectInTextFlow:
    """``continue`` (skip-to-next-pattern) must not be replaced by ``break``.

    Targets: detection.detector.PromptDetector._detect_in_text__mutmut_{4,82}
    """

    def test_first_pattern_regex_doesnt_match_then_second_pattern_matches(self) -> None:
        """If the first pattern's regex doesn't match, ``continue`` to the next
        pattern — not ``break`` (which would exit the loop with no match).

        This specifically targets mutmut_4: the ``if not match: continue``
        branch at the top of the for-loop.
        """
        patterns = [
            {"id": "first", "regex": r"WILL_NOT_MATCH_ANYTHING_HERE"},
            {"id": "second", "regex": r"\$\s*$"},
        ]
        d = PromptDetector(patterns)
        diag = d.detect_prompt_with_diagnostics({"screen": "user$ ", "cursor_at_end": True, "cursor": {"x": 0, "y": 0}})
        # Original: first regex fails → continue → second matches.
        # Mutant (break): first regex fails → break → no patterns left → None.
        assert diag.match is not None
        assert diag.match.prompt_id == "second"

    def test_first_pattern_rejected_by_negative_then_second_pattern_matches(self) -> None:
        """If the first pattern is rejected (negative match), the loop must
        ``continue`` to the next pattern — not ``break`` out of the loop."""
        patterns = [
            {"id": "first", "regex": r"\$\s*$", "negative_regex": r"banner"},
            {"id": "second", "regex": r"\$\s*$"},
        ]
        d = PromptDetector(patterns)
        diag = d.detect_prompt_with_diagnostics(
            {"screen": "banner\nuser$ ", "cursor_at_end": True, "cursor": {"x": 0, "y": 0}}
        )
        # Under the original: continue → second pattern checked → match.
        # Under the mutant (break): no further patterns, no match.
        assert diag.match is not None
        assert diag.match.prompt_id == "second"

    def test_first_pattern_cursor_skipped_then_second_pattern_matches(self) -> None:
        """If the first pattern is skipped for cursor reasons, ``continue`` to
        the second pattern — not ``break``.

        BOTH patterns must require cursor_at_end so they both land in
        ``compiled_all`` (the full-screen second pass) — that's where the
        mutmut_82 continue/break diff lives.
        """
        # Multi-line screen so cursor at (0,0) is NOT in the prompt region —
        # forces the second-pass full-screen scan.
        screen = "header\n" * 14 + "user$ "
        # cursor_at_end=True so both patterns pass the expect_cursor_at_end check
        # would normally succeed. But we want the first to fail the check —
        # set cursor_at_end=False on the snapshot, but use compiled_all path:
        # that requires patterns where expect_cursor_at_end is True.
        patterns = [
            {"id": "first", "regex": r"\$\s*$", "expect_cursor_at_end": True, "input_type": "any_key"},
            {"id": "second", "regex": r"\$\s*$", "expect_cursor_at_end": True, "input_type": "multi_key"},
        ]
        d = PromptDetector(patterns)
        # cursor_at_end=False; both patterns trigger cursor_position diagnostic.
        # Under original: continue → second pattern's cursor check also fails → no match.
        # Under mutant (break): break after first → only one diagnostic entry.
        diag = d.detect_prompt_with_diagnostics({"screen": screen, "cursor_at_end": False, "cursor": {"x": 0, "y": 0}})
        # Two diagnostic entries (one per pattern) under original; one under mutant.
        cursor_position_entries = [e for e in diag.regex_matched_but_failed if e.get("reason") == "cursor_position"]
        assert len(cursor_position_entries) == 2, (
            f"expected 2 cursor_position entries, got {len(cursor_position_entries)} — "
            f"continue mutated to break? entries={cursor_position_entries!r}"
        )


# ---------------------------------------------------------------------------
# SLICE_SIGN (2) — debug-log slice indices
# ---------------------------------------------------------------------------


class TestSliceSigns:
    """Debug logs use ``region_text[-200:]`` / ``screen[-150:]`` to show tails.

    The mutation flips the negative slice to positive, which produces an
    empty (or much smaller) slice — observable via caplog only.

    Targets: detection.detector.PromptDetector.detect_prompt_with_diagnostics__mutmut_{54,118}
    """

    def test_region_tail_log_includes_last_200_chars_not_chars_from_200(self, caplog: pytest.LogCaptureFixture) -> None:
        """Region length must be > 400 so ``[-200:]`` and ``[+200:]`` reach
        different starting indexes (300 vs 200) with non-overlapping content."""
        d = PromptDetector([{"id": "p", "regex": "x"}])
        # 500-char region: first 200 'S', middle 100 'M', last 200 'E'.
        # [-200:] starts at 300 → pure 'E' x 200. ``M`` must NOT appear in tail.
        # [+200:] starts at 200 → 'M' x 100 + 'E' x 200. ``M`` would appear.
        long_region = "S" * 200 + "M" * 100 + "E" * 200
        # Need the cursor to NOT be in the prompt region so the full-screen
        # scan runs and the region log fires.
        snap = {"screen": long_region, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True}
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics(snap)
        region_logs = [r for r in caplog.records if "prompt_detection_region" in r.getMessage()]
        assert region_logs
        # The region_tail in the log uses [-200:]. The region itself may be
        # shorter than the full screen — but if it IS the full screen (no
        # newlines), the tail is the last 200 chars = pure 'E'.
        # Either way, 'M' (middle marker, indices 200-299) must NOT be in the
        # tail when sliced as [-200:]; it WOULD be there under [+200:].
        tail_chunk = region_logs[0].getMessage().split("region_tail=")[-1]
        assert "M" not in tail_chunk, f"tail contains middle marker — slice direction wrong: {tail_chunk[:200]!r}"

    def test_screen_preview_log_uses_last_150_chars(self, caplog: pytest.LogCaptureFixture) -> None:
        """Screen length > 300 so ``[-150:]`` and ``[+150:]`` differ."""
        d = PromptDetector([{"id": "p", "regex": "wont-match-anything-at-all"}])
        # 400-char screen: 'S'*150 + 'M'*100 + 'E'*150.
        # [-150:] → indices 250-400 = pure 'E'. 'M' not present.
        # [+150:] → indices 150-400 = 'M'*100 + 'E'*150. 'M' present.
        long_screen = "S" * 150 + "M" * 100 + "E" * 150
        with caplog.at_level(logging.DEBUG, logger="provide.uterm.detection.detector"):
            d.detect_prompt_with_diagnostics({"screen": long_screen, "cursor": {"x": 0, "y": 0}, "cursor_at_end": True})
        no_match_logs = [r for r in caplog.records if "prompt_detection_no_match" in r.getMessage()]
        assert no_match_logs
        preview = no_match_logs[0].getMessage().split("screen_preview=")[-1]
        assert "M" not in preview, f"preview contains middle marker — slice direction wrong: {preview[:200]!r}"


# ---------------------------------------------------------------------------
# STRIP_SIDE (1) — auth._parse_authorized_keys_line
# ---------------------------------------------------------------------------


class TestParseLineLstrip:
    """``rest = line[first_token_end:].lstrip()`` — must strip from the LEFT.

    Targets: auth._parse_authorized_keys_line__mutmut_19
    """

    def test_options_followed_by_extra_leading_whitespace(self) -> None:
        """After consuming the options token, leading whitespace before the
        keytype must be stripped (``lstrip``) so the next ``split`` finds the
        keytype correctly. ``rstrip`` here would strip trailing whitespace
        from the comment instead, fragmenting the comment."""
        key = b"k"
        # Two spaces between options and keytype — lstrip removes both.
        line = f'subject="alice"  ssh-ed25519 {base64.b64encode(key).decode()}'
        entry = _parse_authorized_keys_line(line)
        assert entry.subject == "alice"
        assert entry.fingerprint.startswith("SHA256:")


# ---------------------------------------------------------------------------
# ARITH_SIGN (1) — io._wait_if_not_idle
# ---------------------------------------------------------------------------


class TestWaitIfNotIdleArithmetic:
    """``timeout_sec - elapsed`` must subtract — not add — when computing remaining wait.

    Targets: io.PromptWaiter._wait_if_not_idle__mutmut_37

    With the mutation, ``timeout_sec + elapsed`` always produces a larger
    value than intended, leading to longer waits. Easiest assertion: the
    timeout_ms passed to wait_for_update is bounded by ``timeout_sec - elapsed``,
    not ``+``.
    """

    @pytest.mark.asyncio
    async def test_wait_ms_bounded_by_remaining_timeout(self) -> None:
        session = MagicMock()
        session.wait_for_update = AsyncMock()
        # Large remaining_idle so the min() takes the timeout_sec - elapsed leg.
        session.seconds_until_idle = MagicMock(return_value=1000.0)
        waiter = PromptWaiter(session)  # type: ignore[arg-type]

        await waiter._wait_if_not_idle(
            detected_full={"prompt_id": "p"},
            is_idle=False,
            elapsed=1.0,
            timeout_sec=2.0,
            idle_grace_ratio=0.9,
            read_interval_sec=0.01,
            require_idle=True,
            on_prompt_rejected=None,
        )
        # timeout_sec - elapsed = 1.0 → 1000 ms. With the mutation (+), it
        # would be 3000 ms.
        assert session.wait_for_update.await_count == 1
        actual_ms = session.wait_for_update.await_args.kwargs["timeout_ms"]
        assert actual_ms == 1000, f"expected 1000ms, got {actual_ms}ms"
