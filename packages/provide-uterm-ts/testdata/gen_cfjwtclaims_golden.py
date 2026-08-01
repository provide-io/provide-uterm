#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the Worker's JWT claim handling.

The Worker is always internet-facing and has no dev bypass: every principal
comes from a cryptographically verified token. What is recorded here is
everything that happens either side of the signature check — splitting the
token, choosing a key from a JWKS, validating the standard claims, and
deriving a subject and roles from what is left.

**A claim that is absent and a claim that is wrong are different failures.**
A token with no ``exp`` is refused outright rather than treated as
non-expiring; a token whose ``iss`` does not match is refused only when an
issuer is configured. Each failure names itself, because the two are diagnosed
differently.

**Clock skew is allowed in both directions and never negative.** A negative
configured skew would otherwise *shorten* a token's life, which is the
opposite of what the setting means.

**A service token is one carrying ``common_name`` and no human identity.** The
presence of an ``email`` claim means a user token, which can never be elevated
as a service token — and even a real service token gets admin only when the
deployment has opted in, because a bare ``common_name`` is too weak a signal
to auto-grant it.

**A browser WebSocket cannot send headers**, so the token also comes from the
``CF_Authorization`` cookie. Both readers are total: anything unreadable
yields no token rather than raising into the request path.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfjwtclaims_golden.py
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from provide.uterm.cloudflare.auth.jwt import (
    JwtValidationError,
    Principal,
    _b64url_decode,
    _check_audience,
    _check_exp,
    _check_issuer,
    _check_nbf,
    _extract_roles,
    _find_jwk,
    _parse_jwt_parts,
    _parse_roles_claim,
    _validate_claims,
    extract_bearer_or_cookie,
    resolve_role,
)
from provide.uterm.cloudflare.config import JwtConfig

OUT = Path(__file__).with_name("cfjwtclaims_golden.json")

# A fixed instant, so the corpus does not move.
NOW = 1_700_000_000.0

BASE = JwtConfig()


def _b64url(raw: bytes) -> str:
    """Encode base64url without padding, as a JWT does."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _segment(value: Any) -> str:
    """One JWT segment carrying JSON."""
    return _b64url(json.dumps(value, separators=(",", ":")).encode())


def _token(header: Any, payload: Any, signature: bytes = b"\x01\x02\x03") -> str:
    """A whole token, whose signature is never checked here."""
    return f"{_segment(header)}.{_segment(payload)}.{_b64url(signature)}"


def _raised(call: Any) -> dict[str, Any]:
    """What a check refuses, and how it says so.

    Only the reference's *own* refusals carry their wording. Everything else
    reaching here came out of the standard library — ``binascii.Error`` from
    the decoder, ``json.JSONDecodeError`` from the parser — and that wording is
    CPython's, not this project's. It has been reworded between releases
    ("number of data characters (1) cannot be 1 more than a multiple of 4" is a
    3.x phrasing, not a specification), and a port has no business reproducing
    it. What the reference decides is *which kind* of failure it is, and the
    exception class already carries that, so the message is dropped rather than
    pinned. Do not record ``str(exc)`` for these.
    """
    try:
        result = call()
    except JwtValidationError as exc:
        return {"error": "JwtValidationError", "message": str(exc), "result": None}
    except Exception as exc:
        return {"error": type(exc).__name__, "message": None, "result": None}
    return {"error": None, "message": None, "result": result}


class _Headers:
    """Request headers, which may be absent or hostile."""

    def __init__(self, data: dict[str, str] | None, *, raises: bool = False) -> None:
        self._data = data or {}
        self._raises = raises

    def get(self, name: str) -> str | None:
        if self._raises:
            raise RuntimeError("headers unreadable")
        return self._data.get(name)


class _Request:
    """A request carrying headers, or not carrying them at all."""

    def __init__(self, headers: Any = None, *, absent: bool = False) -> None:
        if not absent:
            self.headers = headers


# (name, base64url) — what the decoder accepts.
B64_CASES: list[tuple[str, str]] = [
    ("no padding needed", _b64url(b"abcd")),
    ("one pad", _b64url(b"abcde")),
    ("two pads", _b64url(b"abcdef")),
    ("empty", ""),
    # The url alphabet, which differs from standard base64 in two characters.
    ("url-safe characters", _b64url(bytes([0xFB, 0xFF, 0xBE]))),
    # CPython's decoder *discards* characters outside the alphabet rather than
    # refusing them, which is why a header of `!!!` reaches the JSON parser as
    # empty rather than failing to decode. A port reaching for a strict
    # decoder raises here instead, and the wrong error is reported.
    ("not base64 at all", "!!!"),
    ("punctuation among the digits", "YWJ!jZA"),
    ("a single character", "a"),
    ("two characters", "ab"),
    ("three characters", "abc"),
    ("padding alone", "===="),
    ("standard-alphabet characters", "a+b/c"),
    ("both alphabets at once", "a-b_c+d/e"),
    ("whitespace inside", "YW Jj ZA"),
    ("a newline inside", "YWJj\nZA"),
    # Everything after the first pad is ignored, including data characters —
    # a decoder that kept reading past it would decode a different segment.
    ("data after the padding", "YWJj=WJj"),
    # The padding has to complete the group exactly: one pad short is a
    # refusal, not a shorter decode.
    ("one pad short", "YWJjZA="),
    ("padded exactly", "YWJjZA=="),
    ("a stray pad between groups", "YWJj=YWJj"),
    ("a leading pad", "=YWJj"),
]

# Segments the standard library has not made up its mind about, named rather
# than recorded.
#
# ``YW==JjZA`` — a pad in the middle of a group — decoded to ``b"a"`` through
# CPython 3.13.12 and 3.14.3, and raises ``binascii.Error("Incorrect padding")``
# from 3.13.13 and 3.14.4 onwards. 3.12.13, the current 3.12, still accepts it.
# That is a *patch*-level change in ``binascii.a2b_base64``: two interpreters
# with the same minor version disagree, so there is no "the reference does X"
# to record and no version a port could be told to match. Recording the older
# answer is what put this corpus a patch release behind CI, where every runner
# installs the newest patch of its minor — and why the drift check was red on
# the 3.13 and 3.14 cells while 3.11 and 3.12 stayed green.
#
# The behaviour this case was here to pin — that the decoder discards what is
# not in the alphabet, and that the padding arithmetic is computed from the
# original length — is still pinned by ``!!!``, ``YWJ!jZA``, ``====`` and
# ``YWJj=YWJj`` above, all of which every release agrees on. Do not put this
# input back.
B64_UNSTABLE: list[tuple[str, str]] = [
    ("padding in the middle", "YW==JjZA"),
]

# (name, token) — how a token splits.
PARTS_CASES: list[tuple[str, str]] = [
    ("a whole token", _token({"alg": "RS256", "kid": "k1"}, {"sub": "u1"})),
    ("two parts", "a.b"),
    ("four parts", "a.b.c.d"),
    ("no dots at all", "abc"),
    ("empty", ""),
    ("empty parts", ".."),
    ("a header that is not json", f"{_b64url(b'nope')}.{_segment({'sub': 'u1'})}.{_b64url(b'sig')}"),
    ("a payload that is not json", f"{_segment({'alg': 'RS256'})}.{_b64url(b'nope')}.{_b64url(b'sig')}"),
    ("a header that is not base64", f"!!!.{_segment({'sub': 'u1'})}.{_b64url(b'sig')}"),
    # JSON that parses but is not an object. Nothing downstream checks, which
    # is worth pinning rather than assuming.
    ("a header that is a list", f"{_segment([1, 2])}.{_segment({'sub': 'u1'})}.{_b64url(b'sig')}"),
]

# (name, jwks, kid, alg) — which key is chosen.
JWK_CASES: list[tuple[str, dict[str, Any], str | None, str | None]] = [
    ("a matching kid", {"keys": [{"kid": "k1", "alg": "RS256"}, {"kid": "k2"}]}, "k1", "RS256"),
    ("the second key", {"keys": [{"kid": "k0"}, {"kid": "k1", "n": "x"}]}, "k1", None),
    ("a kid that is not there", {"keys": [{"kid": "k0"}]}, "k9", "RS256"),
    ("no kid, matching alg", {"keys": [{"alg": "ES256"}, {"alg": "RS256", "n": "x"}]}, None, "RS256"),
    ("no kid, no alg on the key", {"keys": [{"n": "x"}]}, None, "RS256"),
    ("no kid and no alg at all", {"keys": [{"alg": "ES256", "n": "x"}]}, None, None),
    ("no keys", {"keys": []}, "k1", "RS256"),
    ("no keys field", {}, "k1", "RS256"),
    # A kid takes precedence: a key whose alg matches is not a substitute for
    # the key that was named.
    ("a named kid beats a matching alg", {"keys": [{"alg": "RS256"}, {"kid": "k1"}]}, "k1", "RS256"),
]

# (name, payload, config overrides) — what the standard claims allow.
CLAIM_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("a valid token", {"exp": NOW + 60}, {}),
    ("no exp at all", {}, {}),
    ("expired", {"exp": NOW - 60}, {}),
    ("expired inside the skew", {"exp": NOW - 10}, {}),
    ("expired outside the skew", {"exp": NOW - 31}, {}),
    ("expiring exactly now", {"exp": NOW}, {}),
    ("not yet valid", {"exp": NOW + 60, "nbf": NOW + 60}, {}),
    ("not yet valid inside the skew", {"exp": NOW + 60, "nbf": NOW + 10}, {}),
    ("not yet valid outside the skew", {"exp": NOW + 60, "nbf": NOW + 31}, {}),
    ("valid from exactly now", {"exp": NOW + 60, "nbf": NOW}, {}),
    # A negative configured skew must not shorten a token's life.
    ("expired with a negative skew", {"exp": NOW - 1}, {"clock_skew_seconds": -600}),
    ("valid with a negative skew", {"exp": NOW + 1}, {"clock_skew_seconds": -600}),
    # ``int()`` truncates, so the fractional part of a configured skew is not
    # tolerance the deployment gets.
    ("a fractional skew, truncated away", {"exp": NOW - 30.5}, {"clock_skew_seconds": 30.9}),
    ("a fractional skew, still inside", {"exp": NOW - 29.5}, {"clock_skew_seconds": 30.9}),
    ("an expiry of null", {"exp": None}, {}),
    # Both wrong at once: which failure is reported says which check ran
    # first, and a client reading "not yet valid" would wait rather than
    # re-authenticate.
    ("no expiry and not yet valid", {"nbf": NOW + 60}, {}),
    ("the right issuer", {"exp": NOW + 60, "iss": "https://idp"}, {"issuer": "https://idp"}),
    ("the wrong issuer", {"exp": NOW + 60, "iss": "https://evil"}, {"issuer": "https://idp"}),
    ("no issuer claim but one configured", {"exp": NOW + 60}, {"issuer": "https://idp"}),
    ("an issuer claim with none configured", {"exp": NOW + 60, "iss": "https://any"}, {}),
    ("an empty configured issuer", {"exp": NOW + 60, "iss": "x"}, {"issuer": ""}),
    ("the right audience", {"exp": NOW + 60, "aud": "uterm"}, {"audience": "uterm"}),
    ("the wrong audience", {"exp": NOW + 60, "aud": "other"}, {"audience": "uterm"}),
    ("an audience list containing it", {"exp": NOW + 60, "aud": ["a", "uterm"]}, {"audience": "uterm"}),
    ("an audience list without it", {"exp": NOW + 60, "aud": ["a", "b"]}, {"audience": "uterm"}),
    ("an empty audience list", {"exp": NOW + 60, "aud": []}, {"audience": "uterm"}),
    ("no audience claim but one configured", {"exp": NOW + 60}, {"audience": "uterm"}),
    ("an audience claim with none configured", {"exp": NOW + 60, "aud": "any"}, {}),
]

# (name, raw) — how a roles claim reads.
ROLES_CLAIM_CASES: list[tuple[str, Any]] = [
    ("a list", ["admin", "operator"]),
    ("a comma-separated string", "admin,operator"),
    ("a string with spaces around the commas", " admin , operator "),
    ("a string with empty parts", "admin,,operator,"),
    ("an empty string", ""),
    ("a string of separators", ",,,"),
    ("a list of numbers", [1, 2]),
    ("a list with a null", ["admin", None]),
    ("an empty list", []),
    ("a number", 7),
    ("a boolean", True),
    ("nothing", None),
    ("a mapping", {"admin": True}),
    # A space-separated string is a scope, not a roles list; read as roles it
    # is one role with a space in it.
    ("a space-separated string", "admin operator"),
]

# (name, claims, config overrides) — what roles a token grants.
ROLES_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("roles as a list", {"roles": ["admin"]}, {}),
    ("roles as a string", {"roles": "admin,operator"}, {}),
    ("no roles, a scope instead", {"scope": "operator viewer"}, {}),
    ("no roles and an empty scope", {"scope": ""}, {}),
    ("no roles and a scope that is not a string", {"scope": ["operator"]}, {}),
    ("neither", {}, {}),
    ("a configured default", {}, {"jwt_default_role": "operator"}),
    ("an empty configured default", {}, {"jwt_default_role": ""}),
    ("roles under another claim", {"groups": ["eng"]}, {"jwt_roles_claim": "groups"}),
    ("a scope under another claim", {"scp": "admin"}, {"jwt_scopes_claim": "scp"}),
    ("a mapped group", {"groups": ["eng"]}, {"jwt_roles_claim": "groups", "jwt_role_map": {"eng": "admin"}}),
    ("a group with no mapping", {"groups": ["sales"]}, {"jwt_roles_claim": "groups", "jwt_role_map": {"eng": "admin"}}),
    ("a mapping that empties nothing", {"roles": []}, {"jwt_role_map": {"eng": "admin"}}),
    # Roles win over scope: both present means the roles claim is authoritative.
    ("both roles and scope", {"roles": ["viewer"], "scope": "admin"}, {}),
]

# (name, roles) — the single role a principal resolves to.
RESOLVE_CASES: list[tuple[str, tuple[str, ...]]] = [
    ("admin", ("admin",)),
    ("operator", ("operator",)),
    ("viewer", ("viewer",)),
    ("nothing", ()),
    ("something unknown", ("root",)),
    # The highest wins, whatever order they arrive in.
    ("admin and operator", ("operator", "admin")),
    ("operator and viewer", ("viewer", "operator")),
    ("all three", ("viewer", "operator", "admin")),
]

# (name, request) — where the token comes from.
BEARER_CASES: list[tuple[str, Any]] = [
    ("a bearer header", _Request(_Headers({"Authorization": "Bearer abc.def.ghi"}))),
    ("a lowercase scheme", _Request(_Headers({"Authorization": "bearer abc"}))),
    ("a shouted scheme", _Request(_Headers({"Authorization": "BEARER abc"}))),
    ("a padded token", _Request(_Headers({"Authorization": "Bearer   abc  "}))),
    ("a bearer with no token", _Request(_Headers({"Authorization": "Bearer "}))),
    ("another scheme", _Request(_Headers({"Authorization": "Basic abc"}))),
    ("a scheme that merely starts the same", _Request(_Headers({"Authorization": "Bearerabc"}))),
    ("no authorization at all", _Request(_Headers({}))),
    ("the access cookie", _Request(_Headers({"Cookie": "CF_Authorization=abc"}))),
    ("the cookie among others", _Request(_Headers({"Cookie": "a=1; CF_Authorization=abc; b=2"}))),
    ("a padded cookie", _Request(_Headers({"Cookie": " CF_Authorization = abc "}))),
    ("an empty cookie", _Request(_Headers({"Cookie": "CF_Authorization="}))),
    ("a cookie of another name", _Request(_Headers({"Cookie": "Other=abc"}))),
    ("a cookie whose name merely contains it", _Request(_Headers({"Cookie": "XCF_Authorization=abc"}))),
    ("a cookie value containing an equals sign", _Request(_Headers({"Cookie": "CF_Authorization=a=b"}))),
    # A valueless cookie whose name is one character longer. Read as a pair it
    # would match a name one character shorter and answer with its own name.
    ("a bare cookie before the real one", _Request(_Headers({"Cookie": "CF_Authorizations; CF_Authorization=abc"}))),
    # The header wins: it is what this request presented, where the cookie is
    # whatever the browser had.
    ("both", _Request(_Headers({"Authorization": "Bearer hdr", "Cookie": "CF_Authorization=cke"}))),
    (
        "a bearer with no token, and a cookie",
        _Request(_Headers({"Authorization": "Bearer ", "Cookie": "CF_Authorization=cke"})),
    ),
    ("headers that raise", _Request(_Headers(None, raises=True))),
    ("no headers at all", _Request(absent=True)),
]


def _at_now(call: Any, *args: Any) -> Any:
    """Run a check with the clock held at :data:`NOW`."""
    with patch("time.time", return_value=NOW):
        return call(*args)


def _config(**overrides: Any) -> JwtConfig:
    """The default configuration with a field or two changed."""
    return replace(BASE, **overrides)


def _build() -> dict[str, Any]:
    """Everything the claim handling decides."""
    return {
        "now": NOW,
        "default_clock_skew_seconds": BASE.clock_skew_seconds,
        "default_roles_claim": BASE.jwt_roles_claim,
        "default_scopes_claim": BASE.jwt_scopes_claim,
        "default_role": BASE.jwt_default_role,
        "b64url": [
            {"name": name, "encoded": encoded, **_raised(lambda e=encoded: list(_b64url_decode(e)))}
            for name, encoded in B64_CASES
        ],
        "b64url_unstable": [{"name": name, "encoded": encoded} for name, encoded in B64_UNSTABLE],
        "parts": [
            {
                "name": name,
                "token": token,
                **_raised(lambda t=token: [_parse_jwt_parts(t)[0], _parse_jwt_parts(t)[1]]),
            }
            for name, token in PARTS_CASES
        ],
        "signing_input": _parse_jwt_parts(_token({"alg": "RS256"}, {"sub": "u1"}))[3].decode(),
        "signature_bytes": list(_parse_jwt_parts(_token({"alg": "RS256"}, {"sub": "u1"}, b"\x01\x02\x03"))[2]),
        "jwks": [
            {
                "name": name,
                "jwks": jwks,
                "kid": kid,
                "alg": alg,
                **_raised(lambda j=jwks, k=kid, a=alg: _find_jwk(j, k, a)),
            }
            for name, jwks, kid, alg in JWK_CASES
        ],
        "claims": [
            {
                "name": name,
                "payload": payload,
                "config": overrides,
                # ``_validate_claims`` reads the wall clock, so it is pinned
                # here — otherwise the corpus would record "expired" for every
                # case and stop meaning anything the day after it was written.
                **_raised(lambda p=payload, o=overrides: _at_now(_validate_claims, p, _config(**o))),
            }
            for name, payload, overrides in CLAIM_CASES
        ],
        # Each check on its own, so a port cannot pass by refusing for the
        # wrong reason.
        "exp_alone": _raised(lambda: _check_exp({"exp": NOW - 60}, NOW, 0)),
        "nbf_alone": _raised(lambda: _check_nbf({"nbf": NOW + 60}, NOW, 0)),
        "issuer_alone": _raised(lambda: _check_issuer({"iss": "a"}, _config(issuer="b"))),
        "audience_alone": _raised(lambda: _check_audience({"aud": "a"}, _config(audience="b"))),
        "roles_claim": [
            {"name": name, "raw": raw, "result": list(_parse_roles_claim(raw))} for name, raw in ROLES_CLAIM_CASES
        ],
        "roles": [
            {
                "name": name,
                "claims": claims,
                "config": overrides,
                "result": list(_extract_roles(claims, _config(**overrides))),
            }
            for name, claims, overrides in ROLES_CASES
        ],
        "resolve": [
            {"name": name, "roles": list(roles), "result": resolve_role(Principal(subject_id="u", roles=roles))}
            for name, roles in RESOLVE_CASES
        ],
        "bearer": [{"name": name, "result": extract_bearer_or_cookie(request)} for name, request in BEARER_CASES],
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = _build()
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(CLAIM_CASES)} claim cases, {len(ROLES_CASES)} role cases, {len(BEARER_CASES)} tokens)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
