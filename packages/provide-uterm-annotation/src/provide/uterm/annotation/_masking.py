#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mask credential-shaped text before a match is embedded in a description.

THE RULE (the Go, TypeScript and C# ports copy it byte-for-byte):

A description template that names ``{match}`` receives
``mask_secrets(full_match)[:80]`` -- masked FIRST, then cut. Masking the full
match before the cut means the cut can never leave the head of a credential
behind as a fragment too short for any redaction rule to recognise.

``mask_secrets`` applies ``SECRET_MASKS`` in order, each to the output of the
one before. Every mask has exactly three capture groups -- a kept prefix, the
secret, a kept suffix (often empty) -- and every non-overlapping match, left to
right, is replaced with ``prefix + SECRET_MARKER + suffix``. No mask can match
the empty string, and ``SECRET_MARKER`` re-masks to itself, so the result is
idempotent.

The patterns are deliberately engine-neutral so four regex engines (CPython
``re``, Go RE2, ECMAScript, .NET) agree on every input: ASCII-only classes, no
``\\s``/``\\S``/``\\w`` (whose Unicode reach differs per engine), no
``(?i)`` (Python and RE2 fold the Kelvin sign and long s, ECMAScript does not;
case is spelled out as ``[Bb]``), no lookaround (RE2 has none), and ``^`` only
without multiline. A "token" is a run of anything but ASCII whitespace and
quotes.

Masking widens each credential rule to the WHOLE token (``AKIA`` + 12 or more,
``gh?_`` + 8 or more, the full ``Bearer`` value, the full assignment value) and
adds the shapes the free-form ``{match}`` rules (``conn.curl``, ``conn.wget``)
copy from a command line: ``Authorization:`` headers, ``-u``/``--user``/
``--password`` arguments, URL userinfo, JWTs and a private-key body. It only
changes the description; detection and ``match_key`` (a hash of the full raw
match) are untouched.
"""

from __future__ import annotations

import re

SECRET_MARKER = "[SECRET]"  # noqa: S105 -- the stand-in, not a secret

# Each pattern is written out whole (no shared fragments) so the ports can hold
# the identical strings and annotation_golden.json can pin them.
SECRET_MASKS: tuple[re.Pattern[str], ...] = (
    # An Authorization header's whole value, whatever the scheme.
    re.compile(r"""([Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn][ \t]*:[ \t]*)([^'"\r\n]+)()"""),
    # A Bearer/Basic credential without the header name.
    re.compile(r"""((?:[Bb][Ee][Aa][Rr][Ee][Rr]|[Bb][Aa][Ss][Ii][Cc])[ \t\n\r\f\v]+)([^ \t\n\r\f\v'"]+)()"""),
    # A secret assignment's value: password=, passwd:, secret=, token:, api_key=.
    re.compile(
        r"""((?:[Pp][Aa][Ss][Ss][Ww][Oo][Rr][Dd]|[Pp][Aa][Ss][Ss][Ww][Dd]|[Ss][Ee][Cc][Rr][Ee][Tt]"""
        r"""|[Tt][Oo][Kk][Ee][Nn]|[Aa][Pp][Ii][_-]?[Kk][Ee][Yy])['"]?[ \t]*[=:][ \t]*['"]?)"""
        r"""([^ \t\n\r\f\v'"]+)()"""
    ),
    # A credential-carrying command-line option's argument (curl/wget).
    re.compile(
        r"""((?:^|[ \t])(?:-[uU][ \t]*|--(?:user|proxy-user|password|http-password|ftp-password|proxy-password)"""
        r"""(?:[ \t]+|=))['"]?)([^ \t\n\r\f\v'"]+)()"""
    ),
    # URL userinfo: scheme://user:password@host.
    re.compile(r"""(://)([^/ \t\n\r\f\v@:'"]*:[^/ \t\n\r\f\v@'"]*)(@)"""),
    # The credential rules' own shapes, widened to the whole token.
    re.compile(r"""()(AKIA[0-9A-Z]{12,})()"""),
    re.compile(r"""()(gh[psourx]_[A-Za-z0-9_]{8,})()"""),
    # A JWT anywhere.
    re.compile(r"""()(eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)()"""),
    # A private key's body, after its BEGIN line.
    re.compile(r"""(-----BEGIN[ \t]+[A-Z ]*PRIVATE KEY-----)([^-]+)()"""),
)


def _replace(match: re.Match[str]) -> str:
    return match.group(1) + SECRET_MARKER + match.group(3)


def mask_secrets(text: str) -> str:
    """Return *text* with every credential-shaped substring replaced by ``SECRET_MARKER``."""
    for mask in SECRET_MASKS:
        text = mask.sub(_replace, text)
    return text


__all__ = ["SECRET_MARKER", "SECRET_MASKS", "mask_secrets"]
