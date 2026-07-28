#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for JWT verification and its JWKS cache.

Two halves the claim corpus does not cover: the key cache in front of the
JWKS endpoint, and what a verified token becomes.

**A flapping JWKS endpoint must not take down authentication.** The cache has
a TTL, and when a refresh fails with a usable copy still held, that copy is
served and further attempts are suppressed for a short negative TTL — so a
known-bad endpoint is not re-hit on every request. A first-ever fetch with
nothing to fall back on still fails, because serving nothing is not an option.

**A service token is one carrying ``common_name`` and no human identity.** An
``email`` claim means a user token, which can never be elevated as a service
token however the rest of it reads. Even a real service token gets admin only
where the deployment has opted in: a bare ``common_name`` is too weak a signal
to grant it automatically.

**A token with no subject falls back to its common name**, and one with
neither is refused — an unnamed principal cannot be audited.

The signature check itself is not recorded here. The reference's Web Crypto
path runs only inside the Cloudflare Pyodide runtime and is marked no-cover,
so there is nothing to record; ``crypto.subtle`` is native both in Node and
in a Worker, and the port tests it directly instead.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfjwtverify_golden.py
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from provide.uterm.cloudflare.auth import jwt as jwt_mod
from provide.uterm.cloudflare.config import JwtConfig

OUT = Path(__file__).with_name("cfjwtverify_golden.json")

BASE = JwtConfig(jwks_url="https://idp/.well-known/jwks.json")


def _reset_cache() -> None:
    """Forget everything the module remembers between scenarios."""
    jwt_mod._JWKS_CACHE.clear()
    jwt_mod._JWKS_RETRY_AFTER.clear()


class _Clock:
    """A monotonic clock the scenario advances by hand."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class _Endpoint:
    """A JWKS endpoint that answers, or does not."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.payload: dict[str, Any] = {"keys": [{"kid": "k1"}]}

    async def __call__(self, _url: str) -> dict[str, Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("jwks endpoint down")
        return self.payload


async def _cache_scenario() -> list[dict[str, Any]]:
    """Walk one endpoint through failure and recovery, recording each step."""
    _reset_cache()
    clock = _Clock()
    endpoint = _Endpoint()
    steps: list[dict[str, Any]] = []

    async def step(name: str, *, advance: float = 0.0, fail: bool | None = None) -> None:
        clock.value += advance
        if fail is not None:
            endpoint.fail = fail
        before = endpoint.calls
        try:
            result: Any = await jwt_mod._fetch_jwks(BASE.jwks_url or "")
            error = None
        except Exception as exc:
            result, error = None, type(exc).__name__
        steps.append(
            {
                "name": name,
                "elapsed": clock.value - 1000.0,
                "endpoint_failing": endpoint.fail,
                "fetched": endpoint.calls > before,
                "keys": None if result is None else result.get("keys"),
                "error": error,
            }
        )

    with patch.object(jwt_mod, "_request_jwks", endpoint), patch("time.monotonic", clock):
        await step("the first fetch")
        await step("again, inside the ttl", advance=1.0)
        await step("after the ttl, the endpoint healthy", advance=60.0)
        await step("after the ttl, the endpoint down", advance=61.0, fail=True)
        await step("again, inside the negative ttl", advance=1.0)
        await step("after the negative ttl, still down", advance=5.0)
        await step("after the negative ttl, recovered", advance=6.0, fail=False)
    return steps


async def _first_fetch_failure() -> dict[str, Any]:
    """A first-ever fetch that fails has nothing to fall back on."""
    _reset_cache()
    endpoint = _Endpoint()
    endpoint.fail = True
    with patch.object(jwt_mod, "_request_jwks", endpoint):
        try:
            await jwt_mod._fetch_jwks("https://idp/jwks")
        except Exception as exc:
            return {"error": type(exc).__name__, "calls": endpoint.calls}
    return {"error": None, "calls": endpoint.calls}


async def _separate_urls() -> dict[str, Any]:
    """Two endpoints are cached apart, so one failing does not serve the other."""
    _reset_cache()
    endpoint = _Endpoint()
    with patch.object(jwt_mod, "_request_jwks", endpoint):
        endpoint.payload = {"keys": [{"kid": "a"}]}
        first = await jwt_mod._fetch_jwks("https://a/jwks")
        endpoint.payload = {"keys": [{"kid": "b"}]}
        second = await jwt_mod._fetch_jwks("https://b/jwks")
        again = await jwt_mod._fetch_jwks("https://a/jwks")
    return {"first": first["keys"], "second": second["keys"], "again": again["keys"], "calls": endpoint.calls}


# (name, claims, config overrides) — what a verified token becomes.
PRINCIPAL_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("a user", {"sub": "u1", "roles": ["operator"]}, {}),
    ("a user with no roles", {"sub": "u1"}, {}),
    ("a subject that is a number", {"sub": 7}, {}),
    ("no subject at all", {"roles": ["admin"]}, {}),
    ("an empty subject", {"sub": ""}, {}),
    ("a null subject", {"sub": None}, {}),
    # A service token: a common name and no human identity.
    ("a service token", {"common_name": "ci.example"}, {}),
    ("a service token, admin opted in", {"common_name": "ci.example"}, {"jwt_service_token_admin": True}),
    ("a service token with a subject", {"sub": "svc", "common_name": "ci.example"}, {"jwt_service_token_admin": True}),
    # An email means a human, which can never be elevated as a service token.
    (
        "a common name with an email",
        {"sub": "u1", "common_name": "ci.example", "email": "a@b"},
        {"jwt_service_token_admin": True},
    ),
    (
        "a common name with an empty email",
        {"sub": "u1", "common_name": "ci.example", "email": ""},
        {"jwt_service_token_admin": True},
    ),
    ("a service token with roles of its own", {"common_name": "ci", "roles": ["viewer"]}, {}),
    (
        "a service token with roles, admin opted in",
        {"common_name": "ci", "roles": ["viewer"]},
        {"jwt_service_token_admin": True},
    ),
    ("neither subject nor common name", {"roles": ["admin"]}, {}),
    # No email and no common name is not a service token: without the name
    # there is nothing that identifies it as automation, so an ordinary user
    # token with no email claim must not be elevated.
    (
        "no email and no common name, admin opted in",
        {"sub": "u1", "roles": ["viewer"]},
        {"jwt_service_token_admin": True},
    ),
]


async def _principals() -> list[dict[str, Any]]:
    """What each set of verified claims becomes."""
    out: list[dict[str, Any]] = []
    for name, claims, overrides in PRINCIPAL_CASES:
        config = replace(BASE, **overrides)

        async def _verified(_token: str, _config: Any, captured: Any = claims) -> Any:
            return dict(captured)

        with patch.object(jwt_mod, "_verify_pyjwt", _verified):
            try:
                principal = await jwt_mod.decode_jwt("a.b.c", config)
                record: dict[str, Any] = {
                    "subject_id": principal.subject_id,
                    "roles": list(principal.roles),
                    "error": None,
                }
            except jwt_mod.JwtValidationError as exc:
                record = {"subject_id": None, "roles": None, "error": str(exc)}
        out.append({"name": name, "claims": claims, "config": overrides, **record})
    return out


async def _unconfigured() -> dict[str, Any]:
    """A deployment with no key at all cannot verify anything."""
    try:
        await jwt_mod.decode_jwt("a.b.c", JwtConfig())
    except jwt_mod.JwtValidationError as exc:
        return {"error": str(exc)}
    return {"error": None}


async def _verification_failure() -> dict[str, Any]:
    """A failure inside verification is reported as a validation error."""

    async def _boom(_token: str, _config: Any) -> Any:
        raise RuntimeError("the key was unusable")

    with patch.object(jwt_mod, "_verify_pyjwt", _boom):
        try:
            await jwt_mod.decode_jwt("a.b.c", BASE)
        except jwt_mod.JwtValidationError as exc:
            return {"error": str(exc)}
    return {"error": None}


async def _build() -> dict[str, Any]:
    """Everything verification decides outside the signature check itself."""
    return {
        "cache_ttl_s": jwt_mod._JWKS_CACHE_TTL_S,
        "negative_ttl_s": jwt_mod._JWKS_NEGATIVE_TTL_S,
        "cache_steps": await _cache_scenario(),
        "first_fetch_failure": await _first_fetch_failure(),
        "separate_urls": await _separate_urls(),
        "principals": await _principals(),
        "unconfigured": await _unconfigured(),
        "verification_failure": await _verification_failure(),
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = asyncio.run(_build())
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['cache_steps'])} cache steps, {len(PRINCIPAL_CASES)} principals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
