#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the Cloudflare Worker config.

A Worker is always internet-facing. There is no loopback to fall back on and
no "it is only reachable from inside" — so this module's refusals are the
outermost auth boundary the deployment has, and every one of them is a
deployment that must not start.

**The bearer token must clear an entropy floor, unconditionally.** Not "in
production" — always. A placeholder token on an edge deployment is an open
door, so a known placeholder or anything under 32 characters is refused at
startup rather than served.

**HMAC and asymmetric JWT settings must not be combined.** With both an HS*
algorithm and an asymmetric public key or JWKS URL configured, an attacker
forges an HS* token using the public key bytes as the HMAC secret. The
combination is refused loudly, because the resulting deployment looks fine
and accepts forged tokens.

**There is no open auth mode.** `dev` and `none` are gone: on a Worker they
would be an admin bypass regardless of environment.

**Numeric settings clamp to a floor rather than taking what they are given.**
A zero-byte message limit or a one-second token lifetime is a
misconfiguration that disables a protection; clamping keeps the protection
while letting the operator raise it.

**A malformed role map is ignored, not fatal.** It is metadata for mapping
IdP groups to roles — losing it costs a mapping, and refusing to start over
it costs the whole deployment.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfconfig_golden.py
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.config import CloudflareConfig

OUT = Path(__file__).with_name("cfconfig_golden.json")

# A token that clears the entropy floor, used wherever the config must simply
# build. A corpus fixture, not a credential — nothing has ever accepted it.
GOOD_TOKEN = "K7fQ2xLm9pRt4vWy8zAb3cDe6gHj1kNq"  # noqa: S105

BASE_ENV: dict[str, str] = {"WORKER_BEARER_TOKEN": GOOD_TOKEN}

# (name, env) — configurations the reference accepts.
VALID: list[tuple[str, dict[str, str]]] = [
    ("only what is required", BASE_ENV),
    ("everything at its default", BASE_ENV),
    (
        "every knob set",
        {
            **BASE_ENV,
            "ENVIRONMENT": "production",
            "LOG_LEVEL": "debug",
            "DO_CLASS_NAME": "OtherRuntime",
            "AUTH_MODE": "jwt",
            "JWT_ISSUER": "https://issuer.example",
            "JWT_AUDIENCE": "uterm",
            "JWT_ALGORITHMS": "RS256,ES256",
            "JWT_JWKS_URL": "https://issuer.example/.well-known/jwks.json",
            "JWT_CLOCK_SKEW_SECONDS": "60",
            "JWT_ROLES_CLAIM": "groups",
            "JWT_SCOPES_CLAIM": "scp",
            "JWT_DEFAULT_ROLE": "operator",
            "JWT_ROLE_MAP": '{"engineering": "admin", "ops": "operator"}',
            "JWT_SERVICE_TOKEN_ADMIN": "1",
            "MAX_WS_MESSAGE_BYTES": "2097152",
            "MAX_INPUT_CHARS": "20000",
            "MAX_EVENTS_PER_WORKER": "5000",
            "MAX_BUFFER_BYTES": "2097152",
            "BACKPRESSURE_HIGH_WATER_BYTES": "8388608",
            "BACKPRESSURE_LOW_WATER_BYTES": "2097152",
            "BACKPRESSURE_ACK_GRACE_S": "20.5",
            "UPSTREAM_BASE_WS_URL": "wss://upstream.example/ws",
            "UPSTREAM_CONNECT_TIMEOUT_MS": "5000",
            "UPSTREAM_HEARTBEAT_S": "30",
            "UPSTREAM_MAX_BACKOFF_S": "10",
            "TUNNEL_TOKEN_TTL_S": "7200",
            "TUNNEL_TOKEN_TRANSPORT": "header",
            "TUNNEL_IP_BINDING": "true",
            "SECURITY_MODE": "dev",
            "SECURITY_CSP": "default-src 'self'",
            "SECURITY_HSTS": "max-age=63072000",
            "SECURITY_X_FRAME_OPTIONS": "DENY",
            "SECURITY_X_CONTENT_TYPE_OPTIONS": "nosniff",
            "SECURITY_REFERRER_POLICY": "no-referrer",
            "SECURITY_PERMISSIONS_POLICY": "camera=()",
            "DECKMUX_AUTO_TRANSFER_IDLE_S": "45",
            "DECKMUX_KEYSTROKE_QUEUE": "replay",
            "RESUME_TTL_S": "600",
            "RESUME_ENABLED": "0",
        },
    ),
    # Every numeric floor, approached from below.
    (
        "numbers below their floors",
        {
            **BASE_ENV,
            "MAX_WS_MESSAGE_BYTES": "0",
            "MAX_INPUT_CHARS": "1",
            "MAX_EVENTS_PER_WORKER": "0",
            "MAX_BUFFER_BYTES": "1",
            "BACKPRESSURE_HIGH_WATER_BYTES": "0",
            "BACKPRESSURE_LOW_WATER_BYTES": "-5",
            "BACKPRESSURE_ACK_GRACE_S": "-1",
            "UPSTREAM_CONNECT_TIMEOUT_MS": "0",
            "UPSTREAM_HEARTBEAT_S": "0",
            "UPSTREAM_MAX_BACKOFF_S": "0",
            "JWT_CLOCK_SKEW_SECONDS": "-30",
            "TUNNEL_TOKEN_TTL_S": "1",
            "DECKMUX_AUTO_TRANSFER_IDLE_S": "0",
            "RESUME_TTL_S": "1",
        },
    ),
    ("an unknown security mode falls back to strict", {**BASE_ENV, "SECURITY_MODE": "wide-open"}),
    ("an empty security mode falls back to strict", {**BASE_ENV, "SECURITY_MODE": "   "}),
    ("security mode is case-insensitive", {**BASE_ENV, "SECURITY_MODE": "DEV"}),
    ("an empty auth mode defaults to jwt", {**BASE_ENV, "AUTH_MODE": "   "}),
    ("auth mode is case-insensitive", {**BASE_ENV, "AUTH_MODE": "JWT"}),
    ("an empty algorithm list falls back", {**BASE_ENV, "JWT_ALGORITHMS": " , , "}),
    ("algorithms are trimmed", {**BASE_ENV, "JWT_ALGORITHMS": " RS256 , ES384 "}),
    ("an HMAC algorithm on its own", {**BASE_ENV, "JWT_ALGORITHMS": "HS256"}),
    ("several HMAC algorithms", {**BASE_ENV, "JWT_ALGORITHMS": "HS256,HS512"}),
    # A raw HMAC secret has no PEM header, so it is not an asymmetric key.
    ("an HMAC algorithm with a raw secret", {**BASE_ENV, "JWT_ALGORITHMS": "HS256", "JWT_PUBLIC_KEY_PEM": "sekrit"}),
    ("empty optional strings become absent", {**BASE_ENV, "JWT_ISSUER": "", "JWT_AUDIENCE": "", "JWT_JWKS_URL": ""}),
    # Present-but-empty is different from absent for the security headers.
    ("an empty security header is kept", {**BASE_ENV, "SECURITY_CSP": ""}),
    ("a role map that is not an object is ignored", {**BASE_ENV, "JWT_ROLE_MAP": '["a", "b"]'}),
    ("a role map that is not JSON is ignored", {**BASE_ENV, "JWT_ROLE_MAP": "{not json"}),
    ("an empty role map", {**BASE_ENV, "JWT_ROLE_MAP": "   "}),
    ("role map values are stringified", {**BASE_ENV, "JWT_ROLE_MAP": '{"a": 1, "2": "admin"}'}),
    ("a boolean read from yes", {**BASE_ENV, "TUNNEL_IP_BINDING": "yes"}),
    ("a boolean read from on", {**BASE_ENV, "TUNNEL_IP_BINDING": "ON"}),
    ("a boolean read from anything else is false", {**BASE_ENV, "TUNNEL_IP_BINDING": "maybe"}),
    ("resume disabled", {**BASE_ENV, "RESUME_ENABLED": "no"}),
    ("an empty transport falls back", {**BASE_ENV, "TUNNEL_TOKEN_TRANSPORT": ""}),
    ("an empty queue mode falls back", {**BASE_ENV, "DECKMUX_KEYSTROKE_QUEUE": ""}),
    (
        "empty claim names fall back",
        {**BASE_ENV, "JWT_ROLES_CLAIM": "", "JWT_SCOPES_CLAIM": "", "JWT_DEFAULT_ROLE": ""},
    ),
    ("a boolean with padding", {**BASE_ENV, "TUNNEL_IP_BINDING": "  yes  "}),
    ("a boolean with padding and case", {**BASE_ENV, "RESUME_ENABLED": "  NO  "}),
]

# (name, env) — bindings that are not strings. A Worker binding may arrive as
# a number or a boolean, and the reference stringifies whatever it finds; a
# null means the variable is not set at all.
NON_STRING: list[tuple[str, dict[str, Any]]] = [
    ("a numeric binding", {**BASE_ENV, "MAX_INPUT_CHARS": 20000}),
    ("a boolean binding", {**BASE_ENV, "TUNNEL_IP_BINDING": True}),
    ("a float binding", {**BASE_ENV, "BACKPRESSURE_ACK_GRACE_S": 20.5}),
    ("a null binding", {**BASE_ENV, "JWT_ISSUER": None}),
    ("a null numeric binding falls back", {**BASE_ENV, "MAX_INPUT_CHARS": None}),
]

# (name, env) — configurations the reference refuses to start with.
INVALID: list[tuple[str, dict[str, str]]] = [
    ("no bearer token", {}),
    ("an empty bearer token", {"WORKER_BEARER_TOKEN": ""}),
    ("a placeholder bearer token", {"WORKER_BEARER_TOKEN": "change-me"}),
    ("a placeholder in different case", {"WORKER_BEARER_TOKEN": "ChangeMe"}),
    ("a placeholder with padding", {"WORKER_BEARER_TOKEN": "  placeholder  "}),
    ("a placeholder as a marker inside a longer token", {"WORKER_BEARER_TOKEN": "prefix-replace-me-suffix-padding"}),
    # Placeholders caught by the exact list rather than the substring markers,
    # so the two checks are told apart. Each is padded to clear the length
    # floor, which the exact check runs ahead of.
    ("an exact placeholder, not a marker", {"WORKER_BEARER_TOKEN": "worker-secret"}),
    ("an exact placeholder with padding", {"WORKER_BEARER_TOKEN": "   worker-secret   "}),
    ("another exact placeholder", {"WORKER_BEARER_TOKEN": "dummy-token"}),
    ("a short bearer token", {"WORKER_BEARER_TOKEN": "abc123"}),
    ("a bearer token one character short", {"WORKER_BEARER_TOKEN": "K7fQ2xLm9pRt4vWy8zAb3cDe6gHj1kN"}),
    ("an open auth mode", {**BASE_ENV, "AUTH_MODE": "none"}),
    ("a development auth mode", {**BASE_ENV, "AUTH_MODE": "dev"}),
    ("an unknown auth mode", {**BASE_ENV, "AUTH_MODE": "header"}),
    # Algorithm confusion, in each of its shapes.
    ("HMAC combined with an asymmetric algorithm", {**BASE_ENV, "JWT_ALGORITHMS": "HS256,RS256"}),
    ("HMAC combined with a JWKS URL", {**BASE_ENV, "JWT_ALGORITHMS": "HS256", "JWT_JWKS_URL": "https://x/jwks"}),
    (
        "HMAC combined with a public key PEM",
        {
            **BASE_ENV,
            "JWT_ALGORITHMS": "HS256",
            "JWT_PUBLIC_KEY_PEM": "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----",
        },
    ),
    # Numeric settings are parsed with int()/float(), which raise rather than
    # coerce. A Worker that started with a silently-zeroed limit would have
    # the protection disabled and no sign of it.
    ("a limit that is not a number", {**BASE_ENV, "MAX_WS_MESSAGE_BYTES": "lots"}),
    ("a limit that is a float", {**BASE_ENV, "MAX_INPUT_CHARS": "1000.5"}),
    ("an empty numeric setting", {**BASE_ENV, "UPSTREAM_HEARTBEAT_S": ""}),
    ("a grace period that is not a number", {**BASE_ENV, "BACKPRESSURE_ACK_GRACE_S": "soon"}),
    (
        "HMAC combined with a certificate",
        {
            **BASE_ENV,
            "JWT_ALGORITHMS": "HS512",
            "JWT_PUBLIC_KEY_PEM": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
        },
    ),
]


def _dump(config: CloudflareConfig) -> dict[str, Any]:
    """The config as plain data, with tuples as lists."""
    data = asdict(config)
    data["jwt"]["algorithms"] = list(data["jwt"]["algorithms"])
    return data


def _refusal(env: dict[str, str]) -> dict[str, str]:
    """What the reference says about a configuration it will not start with."""
    try:
        CloudflareConfig.from_env(env)
    except ValueError as exc:
        return {"type": type(exc).__name__, "message": str(exc)}
    raise AssertionError(f"expected {env!r} to be refused")


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "good_token": GOOD_TOKEN,
        "defaults": _dump(CloudflareConfig()),
        "valid": [{"name": name, "env": env, "config": _dump(CloudflareConfig.from_env(env))} for name, env in VALID],
        "invalid": [{"name": name, "env": env, **_refusal(env)} for name, env in INVALID],
        # The mapping may arrive under `env.vars` rather than on `env` itself.
        "non_string": [
            {"name": name, "env": env, "config": _dump(CloudflareConfig.from_env(env))} for name, env in NON_STRING
        ],
        "from_vars_attribute": _dump(CloudflareConfig.from_env(type("E", (), {"vars": BASE_ENV})())),
        "min_bearer_token_chars": 32,
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(VALID)} valid, {len(INVALID)} refused)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
