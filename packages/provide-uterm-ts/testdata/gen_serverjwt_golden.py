#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the server's JWT auth path.

``auth.mode = "dev_token"`` is the mode the live conformance harness runs
every server in, and the reason it is safe to run is that it is *not* a
bypass: ``dev_idp.setup_dev_idp`` mints an HS256 secret and a real JWT, then
rewrites the config to ``mode = "jwt"`` so the token goes through the same
validator a production deployment uses. A port that accepted its own token by
recognising it, rather than by verifying it, would pass every scenario in the
matrix and be an authentication bypass.

So this corpus records that validator: for each token, either the claims that
came out and the principal derived from them, or the error class the reference
raised. Both halves matter — the accepting half proves a token is honoured,
and the refusing half proves each of the ways a token can be wrong is caught.

Two of the tokens are the exact strings the live drivers send as ``auth:
"bad"``, so what the matrix's forged-token steps are held to is recorded here
rather than inferred.

Times are pinned far from now (a fixed year-2100 expiry, a fixed year-2001
one) so the corpus is the same whenever it is regenerated.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_serverjwt_golden.py
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import tempfile
from pathlib import Path
from typing import Any

import jwt
from jwt import api_jwt as jwt_api

from provide.uterm.server.auth import LocalIdentityProvider, extract_bearer_token
from provide.uterm.server.models import AuthConfig

OUT = Path(__file__).resolve().parent / "serverjwt_golden.json"

#: The shared secret every HS256 vector is signed with. Fixed so the corpus is
#: reproducible; 48 url-safe characters, the width ``setup_dev_idp`` mints.
SECRET = "conformance-corpus-secret-do-not-use-in-anything-real"  # noqa: S105  # pragma: allowlist secret
#: A second secret, for the vector whose signature was made by someone else.
OTHER_SECRET = "conformance-corpus-other-secret-also-not-for-real-use"  # noqa: S105  # pragma: allowlist secret

ISSUER = "provide-uterm"
AUDIENCE = "provide-uterm-server"

#: 2100-01-01T00:00:00Z, and 2001-01-01T00:00:00Z. Far enough either side of
#: any plausible run that the clock cannot flip a vector.
FUTURE = 4102444800
PAST = 978307200


def _config(**overrides: Any) -> AuthConfig:
    """The auth config ``setup_dev_idp`` leaves behind, plus any overrides."""
    fields: dict[str, Any] = {
        "mode": "jwt",
        "jwt_public_key_pem": SECRET,
        "jwt_algorithms": ["HS256"],
        "jwt_issuer": ISSUER,
        "jwt_audience": AUDIENCE,
        "clock_skew_seconds": 15,
    }
    fields.update(overrides)
    return AuthConfig(**fields)


def _encode(claims: dict[str, Any], secret: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, secret, algorithm=algorithm)


def _dev_claims(**overrides: Any) -> dict[str, Any]:
    """The claim set ``setup_dev_idp`` mints, before any override."""
    claims: dict[str, Any] = {
        "sub": "dev-user",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": PAST,
        "exp": FUTURE,
        "roles": ["admin"],
    }
    for name, value in overrides.items():
        if value is None:
            claims.pop(name, None)
        else:
            claims[name] = value
    return claims


#: Every token vector: a name, the token itself, and the config it is read
#: under. ``why`` says what the vector is for, because a corpus of opaque
#: strings is a corpus nobody can maintain.
def _vectors() -> list[dict[str, Any]]:
    return [
        {
            "name": "dev_token",
            "token": _encode(_dev_claims()),
            "why": "exactly what setup_dev_idp mints: the token the live harness announces",
        },
        {
            "name": "roles_string",
            "token": _encode(_dev_claims(roles="operator, viewer")),
            "why": "a roles claim written as text rather than a list, split on commas and spaces",
        },
        {
            "name": "roles_unknown",
            "token": _encode(_dev_claims(roles=["superuser", "root"])),
            "why": "roles outside the allow-list are dropped and the least-privileged role stands in",
        },
        {
            "name": "roles_absent",
            "token": _encode(_dev_claims(roles=None)),
            "why": "no roles claim at all is the same as none this server knows",
        },
        {
            "name": "roles_mixed_case",
            "token": _encode(_dev_claims(roles=[" Admin ", "VIEWER"])),
            "why": "roles are trimmed and folded before they are matched",
        },
        {
            "name": "scopes_string",
            "token": _encode(_dev_claims(scope="sessions:read sessions:write")),
            "why": "the scope claim is space-separated when it is text",
        },
        {
            "name": "scopes_list",
            "token": _encode(_dev_claims(scope=["a", " b ", ""])),
            "why": "a scope list is trimmed, and an empty entry is not a scope",
        },
        {
            "name": "tenant",
            "token": _encode(_dev_claims(tenant_id="Acme-Corp")),
            "why": "a tenant claim is canonicalised rather than taken as written",
        },
        {
            "name": "tenant_invalid",
            "token": _encode(_dev_claims(tenant_id="not a tenant!")),
            "why": "a tenant that cannot be canonicalised fails the whole token",
        },
        {
            "name": "expired",
            "token": _encode(_dev_claims(exp=PAST, iat=PAST - 10)),
            "why": "an expiry in the past, past any clock skew",
        },
        {
            "name": "not_yet_valid",
            "token": _encode(_dev_claims(nbf=FUTURE - 10)),
            "why": "a not-before in the future",
        },
        {
            "name": "wrong_issuer",
            "token": _encode(_dev_claims(iss="somebody-else")),
            "why": "a token minted by an issuer this deployment does not trust",
        },
        {
            "name": "wrong_audience",
            "token": _encode(_dev_claims(aud="another-service")),
            "why": "a token minted for a different service, which must not be replayable here",
        },
        {
            "name": "no_audience",
            "token": _encode(_dev_claims(aud=None)),
            "why": "an audience the validator requires and the token does not carry",
        },
        {
            "name": "no_subject",
            "token": _encode(_dev_claims(sub=None)),
            "why": "the sub claim is required outright",
        },
        {
            "name": "blank_subject",
            "token": _encode(_dev_claims(sub="   ")),
            "why": "a subject of nothing but spaces is no subject",
        },
        {
            "name": "no_expiry",
            "token": _encode(_dev_claims(exp=None)),
            "why": "a token that never expires is refused rather than honoured forever",
        },
        {
            "name": "wrong_signature",
            "token": _encode(_dev_claims(), secret=OTHER_SECRET),
            "why": "the right claims signed by the wrong key — the whole point of verifying",
        },
        {
            "name": "algorithm_none",
            "token": _encode(_dev_claims(), secret="", algorithm="none"),
            "why": "the unsigned-token attack: an algorithm outside the configured list",
        },
        {
            "name": "not_a_token",
            "token": "not.a.real.token",
            "why": "what the Python live driver sends as its forged token",
        },
        {
            "name": "not_a_jwt_at_all",
            "token": "uterm-live-conformance-token-no-server-issued",
            "why": "what the TypeScript live driver sends as its forged token: not three parts",
        },
        {
            "name": "empty",
            "token": "",
            "why": "nothing at all",
        },
        {
            "name": "trailing_dot",
            "token": _encode(_dev_claims()) + ".",
            "why": "four parts is not three",
        },
    ]


def _hand_built(header: str, payload: str, signature: str | None = None) -> str:
    """A token assembled segment by segment, for shapes no encoder emits.

    When *signature* is omitted the two segments are signed for real, so the
    vector fails on what it is about rather than on the signature check.
    """
    if signature is None:
        digest = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def _segment(value: Any) -> str:
    """One base64url segment holding *value* as compact JSON."""
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _signed(claims: dict[str, Any]) -> str:
    """A properly signed token, built without the encoder's own claim checks.

    ``jwt.encode`` refuses an issuer that is not a string, an expiry that is
    not a number and so on — but a *decoder* must still have an answer for
    them, because nothing stops an attacker assembling one by hand.
    """
    return _hand_built(_segment({"alg": "HS256", "typ": "JWT"}), _segment(claims))


def _edge_vectors() -> list[dict[str, Any]]:
    """Shapes the minting path cannot produce, read as a whole principal.

    Every one is fixed in time — a year-2100 or year-2001 instant — so the
    corpus is byte-identical whenever it is regenerated. A vector whose verdict
    depended on the wall clock would make the drift gate fail at midnight.
    """
    good = _segment(_dev_claims())
    header = _segment({"alg": "HS256", "typ": "JWT"})
    return [
        {
            "name": "header_not_object",
            "token": _hand_built(_segment([1, 2]), good),
            "why": "a header that parses as JSON and is not a claim set",
        },
        {
            "name": "header_no_alg",
            "token": _hand_built(_segment({"typ": "JWT"}), good),
            "why": "a header naming no algorithm at all",
        },
        {
            "name": "header_alg_empty",
            "token": _hand_built(_segment({"alg": "", "typ": "JWT"}), good),
            "why": "an empty algorithm is not a permitted one",
        },
        {
            "name": "header_alg_asymmetric",
            "token": _hand_built(_segment({"alg": "RS256", "typ": "JWT"}), good),
            "settings": {"jwt_algorithms": ["HS256", "RS256"]},
            "why": "an algorithm the deployment allows, read against a shared secret",
        },
        {
            "name": "header_padding",
            "token": _hand_built("YWJ!jZA", good),
            "why": "a header segment whose padding cannot work out",
        },
        {
            "name": "payload_padding",
            "token": _hand_built(header, "YWJ!jZA"),
            "why": "a payload segment whose padding cannot work out",
        },
        {
            "name": "crypto_padding",
            "token": _hand_built(header, good, "YWJ!jZA"),
            "why": "a signature segment whose padding cannot work out",
        },
        {
            "name": "empty_signature",
            "token": _hand_built(header, good, ""),
            "why": "a signature segment carrying nothing at all, which is shorter than any digest",
        },
        {
            "name": "payload_not_object",
            "token": _hand_built(header, _segment([1, 2])),
            "why": "a correctly signed payload that is not a claim set",
        },
        {
            "name": "iat_in_the_future",
            "token": _signed(_dev_claims(iat=FUTURE)),
            "why": "issued later than now, which is as suspect as not yet valid",
        },
        {
            "name": "iat_not_a_number",
            "token": _signed(_dev_claims(iat="soon")),
            "why": "an issued-at nobody can compare",
        },
        {
            "name": "nbf_not_a_number",
            "token": _signed(_dev_claims(nbf="soon")),
            "why": "a not-before nobody can compare",
        },
        {
            "name": "exp_not_a_number",
            "token": _signed(_dev_claims(exp="never")),
            "why": "an expiry nobody can compare — present, so the required-claim check passes it",
        },
        {
            "name": "iss_not_a_string",
            "token": _signed(_dev_claims(iss=7)),
            "why": "an issuer that is not text",
        },
        {
            "name": "iss_absent",
            "token": _signed(_dev_claims(iss=None)),
            "why": "no issuer at all, where one is configured",
        },
        {
            "name": "aud_list_containing",
            "token": _signed(_dev_claims(aud=["another-service", AUDIENCE])),
            "why": "a token minted for several audiences, one of them this one",
        },
        {
            "name": "aud_not_a_list",
            "token": _signed(_dev_claims(aud={"name": "x"})),
            "why": "an audience claim of a shape the grammar has no reading for",
        },
        {
            "name": "aud_list_of_non_strings",
            "token": _signed(_dev_claims(aud=[1])),
            "why": "an audience list holding something that is not a name",
        },
        {
            "name": "negative_clock_skew",
            "token": _signed(_dev_claims()),
            "settings": {"clock_skew_seconds": -3600},
            "why": "a skew below zero is floored rather than shortening a token's life",
        },
        {
            "name": "roles_not_iterable",
            "token": _signed(_dev_claims(roles=7)),
            "why": "a roles claim of a type that says nothing about roles",
        },
        {
            "name": "scope_not_iterable",
            "token": _signed(_dev_claims(scope=7)),
            "why": "a scope claim of a type that says nothing about scopes",
        },
    ]


#: How ``jwt.decode`` itself answers, one layer below the principal.
#:
#: ``AuthConfig`` types the issuer and the audience as plain strings, so a
#: deployment cannot leave either unset — but the decoder this port ships is a
#: port of ``jwt.decode``, which has a documented reading for both. Recorded
#: here rather than reasoned about, so the branch nobody can reach through the
#: server is still held to the reference.
def _decode_vectors() -> list[dict[str, Any]]:
    base = {"algorithms": ["HS256"], "issuer": ISSUER, "audience": AUDIENCE, "leeway": 15}
    return [
        {
            "name": "issuer_unchecked",
            "token": _signed(_dev_claims(iss="anyone")),
            "args": {**base, "issuer": None},
            "why": "no issuer to check against accepts whichever one arrived",
        },
        {
            "name": "audience_unchecked_with_claim",
            "token": _signed(_dev_claims()),
            "args": {**base, "audience": None},
            "why": "a decoder expecting no audience, handed a token that names one",
        },
        {
            "name": "audience_unchecked_without_claim",
            "token": _signed(_dev_claims(aud=None)),
            "args": {**base, "audience": None},
            "why": "neither side names an audience, which is a match",
        },
        {
            "name": "audience_empty_claim_unchecked",
            "token": _signed(_dev_claims(aud="")),
            "args": {**base, "audience": None},
            "why": "an empty audience claim is no audience rather than one named nothing",
        },
        {
            "name": "expiry_inside_the_skew",
            "token": _signed(_dev_claims(exp=PAST + 10, iat=PAST - 10)),
            "args": {**base, "now": PAST + 20},
            "why": "an expiry a few seconds past still verifies inside the tolerated skew",
        },
        {
            "name": "expiry_outside_the_skew",
            "token": _signed(_dev_claims(exp=PAST + 10, iat=PAST - 10)),
            "args": {**base, "now": PAST + 40},
            "why": "and stops verifying once the skew is used up",
        },
        {
            "name": "nothing_required",
            "token": _signed({"iss": ISSUER, "aud": AUDIENCE}),
            "args": {**base, "require": []},
            "why": "with nothing required, a token carrying neither sub nor exp decodes",
        },
    ]


def _decoded(vector: dict[str, Any]) -> dict[str, Any]:
    """What ``jwt.decode`` makes of one vector, or how it refused it.

    ``now`` is not an argument the reference takes, so a vector that needs a
    pinned clock gets one by patching the module's own clock for the call —
    the same instant the TypeScript decoder is handed.
    """
    args = dict(vector["args"])
    now = args.pop("now", None)
    options = {"require": args.pop("require", ["sub", "exp"])}
    call = lambda: jwt.decode(str(vector["token"]), key=SECRET, options=options, **args)  # noqa: E731
    try:
        if now is None:
            claims = call()
        else:
            with _clock_at(float(now)):
                claims = call()
    except Exception as error:
        return {"accepted": False, "error_type": type(error).__name__, "error": str(error)}
    return {"accepted": True, "claims": claims}


@contextlib.contextmanager
def _clock_at(instant: float) -> Any:
    """Pin the clock PyJWT reads while a vector is decoded."""
    real = jwt_api.datetime

    class _Pinned(real):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return real.fromtimestamp(instant, tz=tz)

    jwt_api.datetime = _Pinned
    try:
        yield
    finally:
        jwt_api.datetime = real


#: What ``setup_dev_idp`` mints, and what it leaves the configuration saying.
#:
#: The secret and the worker token are fresh random material every run, so
#: they are recorded by *shape* — present, and how long — rather than by value.
#: Everything else is exact, because everything else is the contract: the claim
#: names, the subject, the roles, and the collapse of the mode to ``jwt``.
def _dev_idp_cases() -> list[dict[str, Any]]:
    return [
        {"name": "defaults", "config": {}, "options": {}, "why": "what a dev_token deployment issues"},
        {
            "name": "named_issuer",
            "config": {"jwt_issuer": "someone-else", "jwt_audience": "another-service"},
            "options": {},
            "why": "an issuer and audience a config already named are kept",
        },
        {
            "name": "subject_and_roles",
            "config": {},
            "options": {"subject": "someone", "roles": ("operator", "viewer")},
            "why": "who the token is for, and what it may do",
        },
        {"name": "tenant", "config": {}, "options": {"tenant": "acme"}, "why": "a tenant-scoped dev token"},
        {
            "name": "blank_tenant",
            "config": {},
            "options": {"tenant": "   "},
            "why": "a tenant of nothing but spaces is no tenant rather than one named nothing",
        },
        {
            "name": "worker_token_already_set",
            "config": {"worker_bearer_token": "a-configured-worker-token-of-sufficient-width"},
            "options": {},
            "why": "a worker token a config already set is not replaced",
        },
        {"name": "short_ttl", "config": {}, "options": {"ttl_s": 60}, "why": "a token that lives a minute"},
    ]


def _dev_idp(case: dict[str, Any]) -> dict[str, Any]:
    """Run the reference's stub IdP and record what it decided."""
    from provide.uterm.server.dev_idp import setup_dev_idp

    auth = AuthConfig(mode="dev_token", **case["config"])
    with tempfile.TemporaryDirectory() as directory:
        token = setup_dev_idp(auth, token_path=Path(directory) / "dev_token", **case["options"])
    claims = jwt.decode(token, options={"verify_signature": False})
    issued, expiry = int(claims.pop("iat")), int(claims.pop("exp"))
    return {
        "claims": claims,
        "claim_order": list(claims),
        "ttl_s": expiry - issued,
        "auth": {
            "mode": auth.mode,
            "jwt_algorithms": list(auth.jwt_algorithms),
            "jwt_issuer": auth.jwt_issuer,
            "jwt_audience": auth.jwt_audience,
            "secret_length": len(auth.jwt_public_key_pem or ""),
            "worker_bearer_token": auth.worker_bearer_token
            if case["config"].get("worker_bearer_token")
            else len(auth.worker_bearer_token or ""),
        },
        "verifies_against_the_configured_key": _principal(token, auth)["accepted"],
    }


def _principal(token: str, config: AuthConfig) -> dict[str, Any]:
    """What the reference makes of a token, or how it refused it."""
    provider = LocalIdentityProvider(config)
    try:
        principal = provider._principal_from_jwt_token(token)
    except Exception as error:
        return {"accepted": False, "error_type": type(error).__name__, "error": str(error)}
    return {
        "accepted": True,
        "subject_id": principal.subject_id,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
        "scopes": sorted(principal.scopes),
    }


#: How the reference reads an ``Authorization`` header. Recorded because the
#: 401 in every scenario is reached through it: a header this returns nothing
#: for is an anonymous request, whatever it contained.
BEARER_HEADERS: tuple[str, ...] = (
    "Bearer abc",
    "bearer abc",
    "BEARER abc",
    "Bearer  abc",
    "Bearer abc def",
    "Bearer",
    "Bearer ",
    "Basic abc",
    "abc",
    "",
    "   ",
    " Bearer abc ",
)


def main() -> None:
    config = _config()
    vectors = [{**vector, "result": _principal(str(vector["token"]), config)} for vector in _vectors()]

    # The same dev token read under a config that resolved no key at all: the
    # reference refuses rather than skipping the signature check.
    unconfigured = _config(jwt_public_key_pem=None)
    keyless = {
        "name": "no_key_configured",
        "token": str(vectors[0]["token"]),
        "why": "jwt mode with neither a key nor a JWKS url is a refusal, not an unverified accept",
        "result": _principal(str(vectors[0]["token"]), unconfigured),
    }

    edges = [
        {
            **vector,
            "result": _principal(str(vector["token"]), _config(**vector.get("settings", {}))),
        }
        for vector in _edge_vectors()
    ]

    payload = {
        "note": (
            "Recorded from provide.uterm.server.auth.LocalIdentityProvider._principal_from_jwt_token "
            "and extract_bearer_token, under the AuthConfig that dev_idp.setup_dev_idp leaves behind."
        ),
        "secret": SECRET,
        "other_secret": OTHER_SECRET,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "clock_skew_seconds": 15,
        "future_exp": FUTURE,
        "past_exp": PAST,
        "vectors": [*vectors, keyless],
        "edge_vectors": edges,
        "decode_vectors": [{**vector, "result": _decoded(vector)} for vector in _decode_vectors()],
        "dev_idp": [{**case, "result": _dev_idp(case)} for case in _dev_idp_cases()],
        "bearer_headers": [
            {"header": header, "token": extract_bearer_token({"authorization": header})} for header in BEARER_HEADERS
        ],
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {OUT} ({len(payload['vectors'])} + {len(edges)} vectors)")


if __name__ == "__main__":
    main()
