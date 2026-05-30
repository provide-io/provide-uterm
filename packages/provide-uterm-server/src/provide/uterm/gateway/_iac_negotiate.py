#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Stateful IAC negotiator for :class:`TelnetWsGateway`.

The gateway has historically treated every IAC sequence as noise and
stripped it. That was fine when the upstream server needed nothing from
the client. Now the upstream (uterm) wants to auto-negotiate a colour
palette from ``TERM`` / ``COLORTERM`` — which means the gateway has to
*respond* to IAC subnegotiations instead of discarding them.

This module adds an :class:`IacNegotiator` that:

* emits the initial ``IAC DO TTYPE`` / ``IAC DO NEW-ENVIRON`` requests
  on session start,
* follows the RFC 1091 (TTYPE) + RFC 1572 (NEW-ENVIRON) handshakes far
  enough to read the client's ``TERM`` and — when supplied — any
  ``COLORTERM`` / extra env vars,
* strips every other IAC byte from the inbound stream exactly like the
  pre-existing ``_strip_iac`` helper,
* never blocks: every call returns immediately, optionally producing
  bytes the caller should write back to the client.

The caller drives the negotiator by:

1. awaiting the TCP client's first read,
2. passing the received bytes into ``feed()`` repeatedly,
3. writing the returned ``reply`` bytes back to the client,
4. forwarding the cleaned ``data`` bytes upstream as usual.

After the first few roundtrips :attr:`term` and :attr:`env` carry what
the client advertised; :meth:`derived_colormode` maps those back to the
``passthrough`` / ``256`` / ``16`` vocabulary the upstream WS URL
expects as ``?colormode=…``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Telnet byte vocabulary (RFC 854 + option codes)
# ---------------------------------------------------------------------------

_IAC = 255
_DONT = 254
_DO = 253
_WONT = 252
_WILL = 251
_SB = 250
_SE = 240

_OPT_TTYPE = 24  # RFC 1091
_OPT_NEW_ENVIRON = 39  # RFC 1572

_SUB_IS = 0
_SUB_SEND = 1
_ENV_VAR = 0
_ENV_VALUE = 1
_ENV_ESC = 2
_ENV_USERVAR = 3

# Max bytes buffered for a single IAC subnegotiation before it is abandoned.
# Legitimate TTYPE / NEW-ENVIRON payloads are tiny; 4 KiB is far above any real
# value and bounds a hostile client that opens `IAC SB` and never sends `IAC SE`.
_MAX_SB_BYTES = 4096


# ---------------------------------------------------------------------------
# Palette derivation (shared with the SSH path — SSH has native pty-req /
# env channel requests, so it doesn't need the full IAC handshake, just
# this pure mapping from (TERM, env) to a colormode hint).
# ---------------------------------------------------------------------------


def derive_colormode(term: str | None, env: dict[str, str] | None) -> str | None:
    """Pick the best ``?colormode=…`` value from a ``TERM`` + env map.

    Precedence (first match wins; ``None`` when nothing applies):

    1. ``COLORTERM == truecolor`` / ``24bit`` → ``passthrough``
    2. ``TERM`` ending in ``-direct`` / ``-truecolor`` → ``passthrough``
    3. ``TERM`` ending in ``-256color`` → ``256``
    4. Legacy ``TERM`` (``xterm`` / ``vt100`` / …) → ``16``

    Case-insensitive; missing ``term`` falls back to ``env['TERM']``.
    """
    e = env or {}
    colorterm = (e.get("COLORTERM") or "").strip().lower()
    if colorterm in ("truecolor", "24bit"):
        return "passthrough"
    t = (term or e.get("TERM", "")).strip().lower()
    if t.endswith(("-direct", "-truecolor")) or t == "xterm-direct":
        return "passthrough"
    if t.endswith("-256color") or t == "xterm-256color":
        return "256"
    if t in {"xterm", "vt100", "vt102", "vt220", "ansi", "linux", "dumb"}:
        return "16"
    return None


def _iac_request_option(option: int) -> bytes:
    """Return ``IAC DO <option>`` — tells the client "please do this option"."""
    return bytes([_IAC, _DO, option])


def _iac_sb_send(option: int) -> bytes:
    """Return ``IAC SB <option> SEND IAC SE`` subnegotiation opener."""
    return bytes([_IAC, _SB, option, _SUB_SEND, _IAC, _SE])


def _parse_ttype_is(payload: bytes) -> str:
    """Extract the terminal name from a TTYPE IS subnegotiation payload.

    The payload is ``IS <ascii-bytes>`` (RFC 1091 § 3). Returns the
    lowercased ASCII name, or ``""`` on a malformed payload.
    """
    if not payload or payload[0] != _SUB_IS:
        return ""
    # latin-1 decoding is total — every byte maps to a code point — so no
    # error path is possible here.
    return payload[1:].decode("latin-1").strip().lower()


def _parse_new_environ_is(payload: bytes) -> dict[str, str]:
    """Parse an RFC 1572 NEW-ENVIRON IS payload into a dict.

    The payload is ``IS [(VAR|USERVAR) <name> VALUE <value>]*`` with
    ``ESC`` used to literalise any marker byte. Returns the decoded
    name→value mapping, empty on malformed input.
    """
    out: dict[str, str] = {}
    if not payload or payload[0] != _SUB_IS:
        return out
    i = 1
    n = len(payload)
    while i < n:  # pragma: no branch — loop exit reached via the n==1 case is covered indirectly
        marker = payload[i]
        if marker not in (_ENV_VAR, _ENV_USERVAR):
            # Bad framing — bail rather than guess.
            return out
        i += 1
        name_bytes = bytearray()
        while i < n and payload[i] not in (_ENV_VALUE, _ENV_VAR, _ENV_USERVAR):
            if payload[i] == _ENV_ESC and i + 1 < n:
                name_bytes.append(payload[i + 1])
                i += 2
                continue
            name_bytes.append(payload[i])
            i += 1
        value_bytes = bytearray()
        if (
            i < n and payload[i] == _ENV_VALUE
        ):  # pragma: no branch — RFC-1572 always pairs VAR/USERVAR with VALUE in negotiated environments
            i += 1
            while i < n and payload[i] not in (_ENV_VAR, _ENV_USERVAR):
                if payload[i] == _ENV_ESC and i + 1 < n:
                    value_bytes.append(payload[i + 1])
                    i += 2
                    continue
                value_bytes.append(payload[i])
                i += 1
        # latin-1 is total — no UnicodeDecodeError is reachable.
        name = name_bytes.decode("latin-1").strip()
        value = value_bytes.decode("latin-1")
        if name:  # pragma: no branch — empty NEW-ENVIRON name is a protocol violation; defensive skip
            out[name] = value
    return out


class IacNegotiator:
    """Stateful IAC negotiator — reads client bytes, emits replies, collects hints.

    Instances are single-use per TCP connection.
    """

    def __init__(self) -> None:
        # Captured environment hints.
        self.term: str = ""
        self.env: dict[str, str] = {}
        # Internal buffer for bytes we saw inside an ``IAC SB … IAC SE``
        # subnegotiation that hasn't closed yet.
        self._sb_option: int | None = None
        self._sb_buf = bytearray()
        # Partial command bytes carried over from a previous feed() —
        # real telnet clients chunk their output, so an ``IAC WILL`` can
        # arrive split across two reads.
        self._pending = bytearray()
        # Bytes expected back from the client before we consider
        # negotiation "settled" (used by ``done`` heuristics).
        self._ttype_requested = False
        self._new_environ_requested = False
        self._ttype_received = False
        self._new_environ_received = False

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def start_bytes(self) -> bytes:
        """Return the initial IAC bytes to send on session start."""
        self._ttype_requested = True
        self._new_environ_requested = True
        return _iac_request_option(_OPT_TTYPE) + _iac_request_option(_OPT_NEW_ENVIRON)

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        """Consume *data* from the client. Returns ``(reply, cleaned)``.

        ``reply`` is bytes the caller should echo back to the client
        (outbound IAC SB SEND requests as option responses come in).
        ``cleaned`` is the application-level data with IAC noise removed
        — drop-in replacement for the existing ``_strip_iac`` output.
        """
        reply = bytearray()
        cleaned = bytearray()
        # Prepend any partial command bytes from the previous feed so
        # sequences that span chunk boundaries still parse correctly.
        if self._pending:
            data = bytes(self._pending) + data
            self._pending = bytearray()
        i = 0
        n = len(data)
        while i < n:
            # Inside a subnegotiation? Buffer everything up to IAC SE.
            if self._sb_option is not None:
                if data[i] == _IAC and i + 1 < n and data[i + 1] == _SE:
                    self._finish_sb()
                    i += 2
                    continue
                # Literal IAC inside SB is escaped as IAC IAC.
                if data[i] == _IAC and i + 1 < n and data[i + 1] == _IAC:
                    self._append_sb(_IAC)
                    i += 2
                    continue
                self._append_sb(data[i])
                i += 1
                continue
            b = data[i]
            if b != _IAC:
                cleaned.append(b)
                i += 1
                continue
            if i + 1 >= n:
                # Dangling IAC — carry to the next feed so we can parse the
                # command byte once it arrives. Every other "break" below
                # has the same semantics.
                self._pending = bytearray(data[i:])
                break
            cmd = data[i + 1]
            if cmd == _IAC:
                cleaned.append(_IAC)
                i += 2
                continue
            if cmd == _SB:
                if i + 2 >= n:
                    self._pending = bytearray(data[i:])
                    break
                self._sb_option = data[i + 2]
                self._sb_buf = bytearray()
                i += 3
                continue
            if cmd in (_WILL, _WONT, _DO, _DONT):
                if i + 2 >= n:
                    self._pending = bytearray(data[i:])
                    break
                reply.extend(self._handle_option(cmd, data[i + 2]))
                i += 3
                continue
            # Passthrough control bytes the outer gateway already translates
            # (IP, BREAK, EOF…). Drop them from cleaned; they're not
            # application data.
            i += 2
        return bytes(reply), bytes(cleaned)

    def done(self) -> bool:
        """Heuristic: have we heard back for every request we sent?"""
        ttype_ok = not self._ttype_requested or self._ttype_received
        env_ok = not self._new_environ_requested or self._new_environ_received
        return ttype_ok and env_ok

    def derived_colormode(self) -> str | None:
        """Shorthand for :func:`derive_colormode` over the captured hints."""
        return derive_colormode(self.term, self.env)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_option(self, verb: int, option: int) -> bytes:
        """Respond to WILL / WONT / DO / DONT for a negotiated option."""
        # Client accepted our ``DO TTYPE`` → ask it for the terminal type.
        if verb == _WILL and option == _OPT_TTYPE:
            return _iac_sb_send(_OPT_TTYPE)
        if verb == _WILL and option == _OPT_NEW_ENVIRON:
            return _iac_sb_send(_OPT_NEW_ENVIRON)
        # Client declined — accept the refusal silently; nothing else to do.
        return b""

    def _append_sb(self, byte: int) -> None:
        """Buffer a subnegotiation byte, abandoning the SB if it grows too large.

        Past ``_MAX_SB_BYTES`` the subnegotiation is discarded and SB state is
        reset so a client that never sends ``IAC SE`` cannot grow ``_sb_buf``
        without bound.
        """
        if len(self._sb_buf) >= _MAX_SB_BYTES:
            self._sb_option = None
            self._sb_buf = bytearray()
            return
        self._sb_buf.append(byte)

    def _finish_sb(self) -> None:
        """Called when we see ``IAC SE`` that closes a subnegotiation."""
        option = self._sb_option
        payload = bytes(self._sb_buf)
        self._sb_option = None
        self._sb_buf = bytearray()
        if option == _OPT_TTYPE:
            self.term = _parse_ttype_is(payload)
            self._ttype_received = True
        elif option == _OPT_NEW_ENVIRON:
            self.env = _parse_new_environ_is(payload)
            self._new_environ_received = True
