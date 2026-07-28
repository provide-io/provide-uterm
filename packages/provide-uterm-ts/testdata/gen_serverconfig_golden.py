#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript server-config port.

Configuration is where a deployment's security posture is actually set, and a
validator that accepts a bad combination has made that decision for the
operator without telling them.

* **Outbound URLs.** The webhook IdP, the JWKS endpoint and the PAM relay all
  carry HMAC secrets, auth headers, or the keys used to validate admin tokens.
  Cleartext ``http://`` to a routable host is refused; loopback is allowed, so
  local development still works, and any other scheme is refused outright
  rather than being passed to a client that might do something surprising with
  it.
* **Unsatisfiable combinations.** Requiring a signed IdP response with no
  shared secret can never succeed. Refusing it at load time is the difference
  between a server that will not start and one that fails every request at
  runtime — or, worse, silently stops verifying.
* **Mount paths.** Normalised so a path given without a leading slash, or with
  a trailing one, means what the operator meant rather than producing a route
  nothing matches.

The corpus drives the real Pydantic models, so both the decisions and the
messages an operator reads are the reference's.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_serverconfig_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server.config_schema import (
    AuditConfig,
    AuthConfig,
    ControlPlaneConfig,
    PamConfig,
    RecordingConfig,
    ServerBindConfig,
    UiConfig,
    _clean_path,
    _require_secure_url,
)

OUT = Path(__file__).with_name("serverconfig_golden.json")

# (name, url) — the outbound-URL guard.
URL_CASES: list[tuple[str, Any]] = [
    ("https", "https://idp.example.org/decide"),
    ("https on a port", "https://idp.example.org:8443/decide"),
    ("http to loopback by name", "http://localhost:9000/decide"),
    ("http to loopback by address", "http://127.0.0.1:9000/decide"),
    ("http to ipv6 loopback", "http://[::1]:9000/decide"),
    ("http to a .localhost name", "http://idp.localhost/decide"),
    ("http to a routable host", "http://idp.example.org/decide"),
    ("http to a private address", "http://10.0.0.1/decide"),
    ("http to something that ends in localhost", "http://notlocalhost/decide"),
    ("http to a host that merely contains localhost", "http://localhost.evil.example/decide"),
    ("uppercase loopback", "http://LOCALHOST:9000/decide"),
    ("a scheme that is neither", "ftp://idp.example.org/decide"),
    ("no scheme at all", "idp.example.org/decide"),
    ("a file url", "file:///etc/passwd"),
    ("empty", ""),
    ("none", None),
]

# (name, value, fallback) — mount-path normalisation.
PATH_CASES: list[tuple[str, Any, str]] = [
    ("already clean", "/app", "/app"),
    ("no leading slash", "app", "/app"),
    ("trailing slash", "/app/", "/app"),
    ("several trailing slashes", "/app///", "/app"),
    ("padded", "  /app  ", "/app"),
    ("just a slash", "/", "/app"),
    ("empty falls back", "", "/fallback"),
    ("nested", "/a/b/c/", "/app"),
    ("no slashes at all", "app/sub", "/app"),
]

# (name, kwargs) — the auth combinations a validator has an opinion about.
AUTH_CASES: list[tuple[str, dict[str, Any]]] = [
    ("defaults", {}),
    (
        "a proxy secret that is required and present",
        {"require_upstream_proxy_secret": True, "upstream_proxy_secret": "s"},
    ),
    ("a proxy secret that is required and missing", {"require_upstream_proxy_secret": True}),
    (
        "a proxy secret that is required and blank",
        {"require_upstream_proxy_secret": True, "upstream_proxy_secret": "  "},
    ),
    ("a proxy secret nobody required", {"upstream_proxy_secret": "s"}),
    (
        "a webhook idp that must sign, with a secret",
        {"identity_provider": "webhook", "webhook_idp_secret": "s"},
    ),
    ("a webhook idp that must sign, with no secret", {"identity_provider": "webhook"}),
    (
        "a webhook idp that must sign, with a blank secret",
        {"identity_provider": "webhook", "webhook_idp_secret": "   "},
    ),
    (
        "a webhook idp that need not sign",
        {"identity_provider": "webhook", "webhook_idp_require_signed_response": False},
    ),
    ("a local idp with no secret", {"identity_provider": "local"}),
    ("a cleartext webhook url", {"webhook_idp_url": "http://idp.example.org/decide"}),
    ("a cleartext jwks url", {"jwt_jwks_url": "http://keys.example.org/jwks"}),
    ("an unknown field", {"totally_made_up": True}),
]

# (name, model, kwargs) — the other models' validators.
MODEL_CASES: list[tuple[str, Any, dict[str, Any]]] = [
    ("an audit chain with a file", AuditConfig, {"chain_enabled": True, "chain_file": "/var/log/chain.jsonl"}),
    ("an audit chain with nowhere to write", AuditConfig, {"chain_enabled": True}),
    ("an audit chain with a blank file", AuditConfig, {"chain_enabled": True, "chain_file": "   "}),
    ("an audit chain switched off", AuditConfig, {"chain_enabled": False}),
    ("a recording size of zero", RecordingConfig, {"max_bytes": 0}),
    ("a negative recording size", RecordingConfig, {"max_bytes": -1}),
    ("a retention of zero", RecordingConfig, {"retention_s": 0}),
    ("a negative retention", RecordingConfig, {"retention_s": -1}),
    ("a reap interval of zero", ControlPlaneConfig, {"reap_interval_s": 0}),
    ("a negative reap interval", ControlPlaneConfig, {"reap_interval_s": -1}),
    ("a negative reap retention", ControlPlaneConfig, {"reap_retention_s": -1}),
    ("sqlite with a database url", ControlPlaneConfig, {"backend": "sqlite", "database_url": "sqlite:///x.db"}),
    ("sqlite with no database url", ControlPlaneConfig, {"backend": "sqlite"}),
    ("a pam relay over cleartext", PamConfig, {"relay_url": "http://relay.example.org/notify"}),
    ("a pam relay over tls", PamConfig, {"relay_url": "https://relay.example.org/notify"}),
]


def _failure(call: Any) -> str | None:
    """Run `call` and return the first validation message, or None."""
    try:
        call()
    except Exception as exc:  # pydantic raises its own type
        text = str(exc)
        for line in text.splitlines():
            cleaned = line.strip()
            if cleaned.startswith("Value error, "):
                # Pydantic appends its own "[type=..., input_value=...]" tail;
                # the message an operator reads is what comes before it.
                message = cleaned[len("Value error, ") :]
                return message.split(" [type=", 1)[0]
            if "Extra inputs are not permitted" in cleaned:
                return "Extra inputs are not permitted"
        return text.splitlines()[0].strip()
    return None


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "urls": [
            {"name": name, "url": url, "error": _failure(lambda u=url: _require_secure_url(u, "auth.webhook_idp_url"))}
            for name, url in URL_CASES
        ],
        "paths": [
            {"name": name, "value": value, "fallback": fallback, "cleaned": _clean_path(value, fallback)}
            for name, value, fallback in PATH_CASES
        ],
        "auth": [
            {"name": name, "kwargs": kwargs, "error": _failure(lambda k=kwargs: AuthConfig(**k))}
            for name, kwargs in AUTH_CASES
        ],
        "models": [
            {
                "name": name,
                "model": model.__name__,
                "kwargs": kwargs,
                "error": _failure(lambda m=model, k=kwargs: m(**k)),
            }
            for name, model, kwargs in MODEL_CASES
        ],
        "ui_defaults": {
            "app_path": UiConfig().app_path,
            "assets_path": UiConfig().assets_path,
            "normalised_app_path": UiConfig(app_path="app/").app_path,
            "normalised_assets_path": UiConfig(assets_path="assets/").assets_path,
        },
        "bind": {
            "derived_public_base_url": ServerBindConfig(host="0.0.0.0", port=8080).public_base_url,
            "explicit_public_base_url": ServerBindConfig(
                host="0.0.0.0",
                port=8080,
                public_base_url="https://uterm.example.org",
            ).public_base_url,
        },
        "auth_defaults": {
            "mode": AuthConfig().mode,
            "identity_provider": AuthConfig().identity_provider,
            "webhook_idp_on_failure": AuthConfig().webhook_idp_on_failure,
            "webhook_idp_require_signed_response": AuthConfig().webhook_idp_require_signed_response,
            "webhook_idp_require_response_nonce": AuthConfig().webhook_idp_require_response_nonce,
            "delegate_roles": AuthConfig().delegate_roles,
            "trusted_proxy_ips": AuthConfig().trusted_proxy_ips,
            "allow_adhoc_browser_observers": AuthConfig().allow_adhoc_browser_observers,
            "clock_skew_seconds": AuthConfig().clock_skew_seconds,
            "jwt_algorithms": AuthConfig().jwt_algorithms,
        },
        "loopback_hosts": sorted(
            __import__("provide.uterm.server.config_schema", fromlist=["_LOOPBACK_HOSTS"])._LOOPBACK_HOSTS
        ),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(URL_CASES)} urls, {len(AUTH_CASES)} auth cases, {len(MODEL_CASES)} model cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
