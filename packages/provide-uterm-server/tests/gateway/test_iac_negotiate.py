#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for the IAC TTYPE + NEW-ENVIRON negotiator."""

from __future__ import annotations

import pytest

from provide.uterm.gateway._iac_negotiate import (
    IacNegotiator,
    _parse_new_environ_is,
    _parse_ttype_is,
    derive_colormode,
)

pytestmark = pytest.mark.unit

# Telnet byte vocabulary, restated for readability in tests.
IAC = 255
DO = 253
WILL = 251
SB = 250
SE = 240
TTYPE = 24
NEW_ENVIRON = 39
SUB_IS = 0
SUB_SEND = 1
ENV_VAR = 0
ENV_VALUE = 1
ENV_USERVAR = 3
ENV_ESC = 2


def _ttype_is(term: str) -> bytes:
    return bytes([IAC, SB, TTYPE, SUB_IS]) + term.encode("latin-1") + bytes([IAC, SE])


def _new_environ_is(vars_: dict[str, str]) -> bytes:
    buf = bytearray([IAC, SB, NEW_ENVIRON, SUB_IS])
    for name, value in vars_.items():
        buf.append(ENV_VAR)
        buf.extend(name.encode("latin-1"))
        buf.append(ENV_VALUE)
        buf.extend(value.encode("latin-1"))
    buf.extend([IAC, SE])
    return bytes(buf)


class TestStartBytes:
    def test_start_bytes_requests_both_options(self) -> None:
        n = IacNegotiator()
        assert n.start_bytes() == bytes([IAC, DO, TTYPE, IAC, DO, NEW_ENVIRON])


class TestFeedTtype:
    def test_will_ttype_triggers_sb_send_reply(self) -> None:
        n = IacNegotiator()
        n.start_bytes()
        reply, cleaned = n.feed(bytes([IAC, WILL, TTYPE]))
        # Gateway should follow up by asking for the actual terminal name.
        assert reply == bytes([IAC, SB, TTYPE, SUB_SEND, IAC, SE])
        assert cleaned == b""

    def test_ttype_is_captures_term_lowercased(self) -> None:
        n = IacNegotiator()
        n.feed(_ttype_is("XTERM-256COLOR"))
        assert n.term == "xterm-256color"

    def test_malformed_ttype_is_returns_empty_string(self) -> None:
        # Missing the SUB_IS marker — should not raise, just leaves term empty.
        n = IacNegotiator()
        # Manually craft an invalid subnegotiation payload.
        n.feed(bytes([IAC, SB, TTYPE]) + b"BAD-PAYLOAD" + bytes([IAC, SE]))
        assert n.term == ""


class TestFeedNewEnviron:
    def test_will_new_environ_triggers_sb_send_reply(self) -> None:
        n = IacNegotiator()
        n.start_bytes()
        reply, _ = n.feed(bytes([IAC, WILL, NEW_ENVIRON]))
        assert reply == bytes([IAC, SB, NEW_ENVIRON, SUB_SEND, IAC, SE])

    def test_new_environ_is_captures_colorterm(self) -> None:
        n = IacNegotiator()
        n.feed(_new_environ_is({"COLORTERM": "truecolor", "TERM": "xterm-256color"}))
        assert n.env == {"COLORTERM": "truecolor", "TERM": "xterm-256color"}

    def test_uservar_treated_same_as_var(self) -> None:
        buf = bytearray([IAC, SB, NEW_ENVIRON, SUB_IS, ENV_USERVAR])
        buf.extend(b"LANG")
        buf.append(ENV_VALUE)
        buf.extend(b"en_US.UTF-8")
        buf.extend([IAC, SE])
        n = IacNegotiator()
        n.feed(bytes(buf))
        assert n.env == {"LANG": "en_US.UTF-8"}

    def test_malformed_marker_falls_back_to_empty(self) -> None:
        # Invalid marker byte after IS — should not raise, just zero hints.
        buf = bytearray([IAC, SB, NEW_ENVIRON, SUB_IS, 42])
        buf.extend(b"name")
        buf.extend([IAC, SE])
        n = IacNegotiator()
        n.feed(bytes(buf))
        assert n.env == {}

    def test_parse_new_environ_is_empty_payload(self) -> None:
        assert _parse_new_environ_is(b"") == {}
        assert _parse_new_environ_is(bytes([99])) == {}  # no SUB_IS prefix

    def test_env_esc_escapes_marker_byte_in_name_and_value(self) -> None:
        """RFC 1572 §5 — ESC literalises a following marker byte.

        Client legitimately wants a variable named ``FOO\\x00BAR`` (with a
        literal VAR byte in the middle) and a value containing a literal
        VALUE byte: both are transmitted as ``ESC <byte>`` so the parser
        doesn't treat them as framing markers.
        """
        buf = bytearray([IAC, SB, NEW_ENVIRON, SUB_IS, ENV_VAR])
        buf.extend(b"FOO")
        buf.extend([ENV_ESC, ENV_VAR])  # literal VAR byte inside the name
        buf.extend(b"BAR")
        buf.append(ENV_VALUE)
        buf.extend(b"hi")
        buf.extend([ENV_ESC, ENV_VALUE])  # literal VALUE byte inside the value
        buf.extend(b"bye")
        buf.extend([IAC, SE])
        n = IacNegotiator()
        n.feed(bytes(buf))
        assert n.env == {f"FOO{chr(ENV_VAR)}BAR": f"hi{chr(ENV_VALUE)}bye"}


class TestCleanedApplicationData:
    def test_strips_iac_will_wont_do_dont(self) -> None:
        n = IacNegotiator()
        # Mix a real byte, a WILL sequence, a DO sequence, and a real byte.
        data = bytes([ord("a"), IAC, WILL, 23, ord("b"), IAC, DO, 99, ord("c")])
        _, cleaned = n.feed(data)
        assert cleaned == b"abc"

    def test_passes_plain_bytes_through(self) -> None:
        n = IacNegotiator()
        _, cleaned = n.feed(b"hello world")
        assert cleaned == b"hello world"

    def test_iac_iac_escapes_to_single_iac(self) -> None:
        n = IacNegotiator()
        _, cleaned = n.feed(bytes([ord("x"), IAC, IAC, ord("y")]))
        assert cleaned == bytes([ord("x"), IAC, ord("y")])

    def test_split_negotiation_across_feeds_survives(self) -> None:
        """IAC byte arrives at end of one feed; rest on the next feed."""
        n = IacNegotiator()
        # First chunk ends mid-WILL sequence — negotiator should not crash
        # on the truncation; it simply breaks out of the loop and waits.
        reply_a, cleaned_a = n.feed(bytes([ord("a"), IAC, WILL]))
        assert cleaned_a == b"a"
        reply_b, cleaned_b = n.feed(bytes([TTYPE, ord("b")]))
        assert cleaned_b == b"b"

    def test_sb_body_across_multiple_feeds(self) -> None:
        """Subnegotiation payload split across two feeds — final value OK."""
        n = IacNegotiator()
        n.feed(bytes([IAC, SB, TTYPE, SUB_IS]) + b"XTERM")
        n.feed(b"-256COLOR" + bytes([IAC, SE]))
        assert n.term == "xterm-256color"

    def test_trailing_iac_alone_carried_to_next_feed(self) -> None:
        """Feed ends at exactly IAC with no command byte yet.

        Distinct from the IAC WILL/WONT/DO/DONT split — this exercises
        the i+1 >= n branch where even the command byte is missing.
        """
        n = IacNegotiator()
        reply_a, cleaned_a = n.feed(bytes([ord("x"), IAC]))
        assert cleaned_a == b"x"
        assert reply_a == b""
        # Next feed delivers WILL TTYPE — should now parse.
        reply_b, _ = n.feed(bytes([WILL, TTYPE]))
        assert reply_b == bytes([IAC, SB, TTYPE, SUB_SEND, IAC, SE])

    def test_iac_sb_without_option_byte_is_carried(self) -> None:
        """IAC SB split across feeds BEFORE the option byte arrives.

        If the first feed contains only ``IAC SB`` (no option number yet),
        the negotiator must defer the command until the next chunk rather
        than reading an application byte as an option code.
        """
        n = IacNegotiator()
        reply_a, cleaned_a = n.feed(bytes([ord("a"), IAC, SB]))
        assert cleaned_a == b"a"
        assert reply_a == b""
        # Now complete the SB: option=TTYPE, IS, "XTERM", IAC SE.
        n.feed(bytes([TTYPE, SUB_IS]) + b"XTERM" + bytes([IAC, SE]))
        assert n.term == "xterm"

    def test_iac_sb_escaped_iac_in_subnegotiation_body(self) -> None:
        """IAC IAC inside an SB body is a literal 0xFF — not end-of-SB."""
        # Craft a NEW-ENVIRON IS with a literal 0xFF byte in the value.
        buf = bytearray([IAC, SB, NEW_ENVIRON, SUB_IS, ENV_VAR])
        buf.extend(b"X")
        buf.append(ENV_VALUE)
        buf.extend([IAC, IAC])  # escaped 0xFF
        buf.extend(b"Z")
        buf.extend([IAC, SE])
        n = IacNegotiator()
        n.feed(bytes(buf))
        assert n.env == {"X": "\xffZ"}

    def test_unknown_iac_command_is_dropped(self) -> None:
        """IAC NOP (241) / AYT (246) / etc are swallowed from cleaned output.

        These are transport-layer control bytes that the outer gateway
        handles (or safely ignores). They must NOT leak into application
        data as literal bytes.
        """
        IAC_NOP = 241
        IAC_AYT = 246
        n = IacNegotiator()
        _, cleaned = n.feed(bytes([ord("a"), IAC, IAC_NOP, ord("b"), IAC, IAC_AYT, ord("c")]))
        assert cleaned == b"abc"

    def test_finish_sb_with_unknown_option_is_silent(self) -> None:
        """Closing an SB for an option we never requested must not crash.

        A real client might advertise options we're not tracking (e.g.
        TSPEED=32, XDISPLOC=35). The negotiator should buffer the body,
        see IAC SE, and move on — leaving term/env untouched.
        """
        TSPEED = 32
        payload = bytes([IAC, SB, TSPEED, SUB_IS]) + b"38400,38400" + bytes([IAC, SE])
        n = IacNegotiator()
        n.feed(payload)
        assert n.term == ""
        assert n.env == {}


class TestDone:
    def test_done_false_before_any_responses(self) -> None:
        n = IacNegotiator()
        n.start_bytes()
        assert n.done() is False

    def test_done_true_after_both_is_responses(self) -> None:
        n = IacNegotiator()
        n.start_bytes()
        n.feed(_ttype_is("xterm"))
        n.feed(_new_environ_is({"COLORTERM": "truecolor"}))
        assert n.done() is True

    def test_done_true_if_no_requests_ever_sent(self) -> None:
        """Negotiator that never called start_bytes() is trivially 'done'."""
        n = IacNegotiator()
        assert n.done() is True


class TestDerivedColormode:
    def test_colorterm_truecolor_wins(self) -> None:
        n = IacNegotiator()
        n.env = {"COLORTERM": "TrueColor"}
        n.term = "xterm"  # legacy — would normally mean 16, but COLORTERM overrides
        assert n.derived_colormode() == "passthrough"

    def test_colorterm_24bit_also_passthrough(self) -> None:
        n = IacNegotiator()
        n.env = {"COLORTERM": "24bit"}
        assert n.derived_colormode() == "passthrough"

    def test_xterm_direct_returns_passthrough(self) -> None:
        n = IacNegotiator()
        n.term = "xterm-direct"
        assert n.derived_colormode() == "passthrough"

    def test_term_256color_returns_256(self) -> None:
        n = IacNegotiator()
        n.term = "tmux-256color"
        assert n.derived_colormode() == "256"

    def test_xterm_256color_returns_256(self) -> None:
        n = IacNegotiator()
        n.term = "xterm-256color"
        assert n.derived_colormode() == "256"

    @pytest.mark.parametrize("t", ["xterm", "vt100", "vt102", "vt220", "ansi", "linux", "dumb"])
    def test_legacy_terms_return_16(self, t: str) -> None:
        n = IacNegotiator()
        n.term = t
        assert n.derived_colormode() == "16"

    def test_unknown_term_returns_none(self) -> None:
        n = IacNegotiator()
        n.term = "alacritty"
        assert n.derived_colormode() is None

    def test_no_term_no_env_returns_none(self) -> None:
        assert IacNegotiator().derived_colormode() is None

    def test_env_term_used_when_ttype_missing(self) -> None:
        """When TTYPE didn't land but NEW-ENVIRON carried TERM, use that."""
        n = IacNegotiator()
        n.env = {"TERM": "xterm-256color"}
        assert n.derived_colormode() == "256"


class TestParseTtypeIs:
    def test_happy(self) -> None:
        assert _parse_ttype_is(bytes([SUB_IS]) + b"Xterm") == "xterm"

    def test_no_is_prefix(self) -> None:
        assert _parse_ttype_is(b"xterm") == ""

    def test_empty(self) -> None:
        assert _parse_ttype_is(b"") == ""


class TestDeriveColormodeSharedHelper:
    """The standalone :func:`derive_colormode` is the pure version of the
    negotiator's method — used directly by the SSH path, which gets
    TERM + env from asyncssh's pty-req / env-channel handlers rather than
    an IAC subnegotiation. Same rules apply in both transports."""

    def test_colorterm_passthrough(self) -> None:
        assert derive_colormode("xterm", {"COLORTERM": "truecolor"}) == "passthrough"
        assert derive_colormode(None, {"COLORTERM": "24bit"}) == "passthrough"

    def test_term_256color(self) -> None:
        assert derive_colormode("tmux-256color", {}) == "256"
        assert derive_colormode("xterm-256color", None) == "256"

    def test_term_direct_is_passthrough(self) -> None:
        assert derive_colormode("xterm-direct", {}) == "passthrough"
        assert derive_colormode("tmux-truecolor", {}) == "passthrough"

    def test_legacy_terms(self) -> None:
        for t in ("xterm", "vt100", "vt102", "vt220", "ansi", "linux", "dumb"):
            assert derive_colormode(t, {}) == "16", t

    def test_unknown_returns_none(self) -> None:
        assert derive_colormode("kitty", {}) is None
        assert derive_colormode(None, None) is None
        assert derive_colormode("", {}) is None

    def test_env_term_wins_when_direct_arg_missing(self) -> None:
        assert derive_colormode(None, {"TERM": "xterm-256color"}) == "256"

    def test_case_insensitive(self) -> None:
        assert derive_colormode("XTERM-256COLOR", {}) == "256"
        assert derive_colormode(None, {"COLORTERM": "TrueColor"}) == "passthrough"


class TestDerivedColormodeStillWorks:
    """Regression: the instance method delegates to the shared helper."""

    def test_delegates_to_shared_helper(self) -> None:
        n = IacNegotiator()
        n.term = "xterm-256color"
        assert n.derived_colormode() == derive_colormode("xterm-256color", {})


class TestSbBufferCap:
    def test_unterminated_subnegotiation_is_bounded(self) -> None:
        from provide.uterm.gateway._iac_negotiate import _MAX_SB_BYTES, IacNegotiator

        neg = IacNegotiator()
        neg.feed(b"\xff\xfa\x18")  # IAC SB TTYPE
        neg.feed(b"A" * (_MAX_SB_BYTES * 4))
        assert len(neg._sb_buf) <= _MAX_SB_BYTES
        assert neg._sb_option is None

    def test_legitimate_small_subnegotiation_still_parses(self) -> None:
        """A well-formed TTYPE IS within the cap parses normally after the cap is in place."""
        n = IacNegotiator()
        n.feed(bytes([IAC, SB, TTYPE, SUB_IS]) + b"xterm-256color" + bytes([IAC, SE]))
        assert n.term == "xterm-256color"
