#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for per-field config validation.

The cross-field rules are already recorded by ``gen_serverconfig_golden.py``.
This corpus covers the layer underneath them: what each individual field will
*accept*.

That layer is load-bearing for the same reason the cross-field rules are. A
config file is where a deployment's posture is set, and a field that quietly
takes a value it should not have taken has made a decision on the operator's
behalf:

* **A closed set of choices.** ``security.mode``, ``auth.identity_provider``,
  ``webhook_idp_on_failure`` and the rest are enumerations, and every one of
  them names a *less* safe option alongside the default. A value outside the
  set must be refused rather than falling back, because falling back picks one
  of those options without saying so.
* **A field nobody defined.** Every section forbids extras, so a typo is a
  startup failure rather than a setting that silently does nothing — including
  a security setting the operator believes is on.
* **A type that is not the field's.** Refused rather than coerced from
  anything, so ``metrics_require_auth = "no"`` cannot read as true.

The corpus drives the real Pydantic models, so what is recorded is what the
reference accepts, the error class it raises, and where.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_configschema_golden.py
"""

from __future__ import annotations

import json
import types
import typing
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from provide.uterm.server import config_schema as schema

OUT = Path(__file__).resolve().parent / "configschema_golden.json"

MODELS: dict[str, type[BaseModel]] = {
    "AuthConfig": schema.AuthConfig,
    "AuditConfig": schema.AuditConfig,
    "UiConfig": schema.UiConfig,
    "RecordingConfig": schema.RecordingConfig,
    "ControlPlaneConfig": schema.ControlPlaneConfig,
    "SecurityConfig": schema.SecurityConfig,
    "TunnelConfig": schema.TunnelConfig,
    "WebhooksConfig": schema.WebhooksConfig,
    "ProfileStoreConfig": schema.ProfileStoreConfig,
    "ServerBindConfig": schema.ServerBindConfig,
    "PamConfig": schema.PamConfig,
    "GovernanceConfig": schema.GovernanceConfig,
    "GraphicalTargetConfig": schema.GraphicalTargetConfig,
}


def _describe(annotation: Any) -> dict[str, Any]:
    """Classify one field's annotation into the shape the TypeScript spec uses."""
    origin = typing.get_origin(annotation)
    if origin is Literal:
        return {"kind": "literal", "choices": list(typing.get_args(annotation))}
    if origin in (types.UnionType, typing.Union):
        args = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        described = _describe(args[0])
        described["optional"] = True
        return described
    if origin is list:
        return {"kind": "list", "item": _describe(typing.get_args(annotation)[0])}
    if origin is dict:
        return {"kind": "dict"}
    if annotation is bool:
        return {"kind": "bool"}
    if annotation is int:
        return {"kind": "int"}
    if annotation is float:
        return {"kind": "float"}
    if annotation is str:
        return {"kind": "str"}
    if annotation is Path:
        return {"kind": "path"}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {"kind": "model", "name": annotation.__name__}
    return {"kind": "unknown", "name": getattr(annotation, "__name__", str(annotation))}


def _spec(model: type[BaseModel]) -> dict[str, Any]:
    """The field spec the port has to reproduce, read off the model itself."""
    fields: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        described = _describe(field.annotation)
        described.setdefault("optional", False)
        fields[name] = described
    return fields


def _outcome(model: type[BaseModel], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Whether the reference accepts these values, and what it says if not."""
    try:
        instance = model(**kwargs)
    except ValidationError as exc:
        return {
            "errors": [
                {"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()
            ]
        }
    accepted = json.loads(instance.model_dump_json())
    # The default session definition stamps its own creation time, which would
    # make this corpus differ from itself on every run. Dropped rather than
    # frozen: nothing here is about when a session was made.
    for session in accepted.get("sessions", []):
        session.pop("created_at", None)
    return {"accepted": accepted}


# Cases chosen for the decision each one pins, not for coverage of the type
# system: every enumeration is probed just outside its set, every boolean with
# the string that reads as its opposite, and every section with a name nobody
# defined.
CASES: list[tuple[str, str, dict[str, Any]]] = [
    ("SecurityConfig", "the default posture", {}),
    ("SecurityConfig", "the relaxed header set, named", {"mode": "dev"}),
    ("SecurityConfig", "a mode nobody defined", {"mode": "relaxed"}),
    ("SecurityConfig", "a mode in the wrong case", {"mode": "STRICT"}),
    ("SecurityConfig", "a mode that is not a string", {"mode": 1}),
    ("SecurityConfig", "a session visibility outside the set", {"default_session_visibility": "everyone"}),
    ("SecurityConfig", "a visibility the set has", {"default_session_visibility": "private"}),
    ("SecurityConfig", "a boolean written as a word", {"metrics_require_auth": "no"}),
    ("SecurityConfig", "a boolean written as the string true", {"metrics_require_auth": "true"}),
    ("SecurityConfig", "a boolean written as a number", {"metrics_require_auth": 1}),
    ("SecurityConfig", "a boolean given a list", {"metrics_require_auth": []}),
    ("SecurityConfig", "a boolean given a number outside the set", {"metrics_require_auth": 2}),
    ("SecurityConfig", "a boolean given a word that is not one", {"metrics_require_auth": "maybe"}),
    ("SecurityConfig", "a boolean written in capitals", {"metrics_require_auth": "TRUE"}),
    ("SecurityConfig", "a boolean written as off", {"metrics_require_auth": "off"}),
    ("SecurityConfig", "a boolean given null", {"metrics_require_auth": None}),
    ("SecurityConfig", "a field nobody defined", {"metrics_require_authh": True}),
    ("SecurityConfig", "two things wrong at once", {"mode": "relaxed", "metrics_require_auth": []}),
    ("SecurityConfig", "a literal given null", {"mode": None}),
    (
        "SecurityConfig",
        "two things wrong, written the other way round",
        {"metrics_require_auth": [], "mode": "relaxed"},
    ),
    (
        "SecurityConfig",
        "a name nobody defined alongside a bad value",
        {"metrics_require_authh": True, "mode": "relaxed"},
    ),
    ("SecurityConfig", "a header given as null", {"csp": None}),
    ("SecurityConfig", "a header given as a number", {"csp": 5}),
    ("AuthConfig", "an identity provider outside the set", {"identity_provider": "ldap"}),
    ("AuthConfig", "a failure mode outside the set", {"webhook_idp_on_failure": "allow"}),
    ("AuthConfig", "the fail-open failure mode, named", {"webhook_idp_on_failure": "viewer"}),
    ("AuthConfig", "a clock skew that is not a number", {"clock_skew_seconds": "fifteen"}),
    ("AuthConfig", "a clock skew written as a string", {"clock_skew_seconds": "30"}),
    ("AuthConfig", "a clock skew written as a whole float", {"clock_skew_seconds": 30.0}),
    ("AuthConfig", "a clock skew written as a fraction", {"clock_skew_seconds": 30.5}),
    ("AuthConfig", "a clock skew given a boolean", {"clock_skew_seconds": True}),
    ("AuthConfig", "a clock skew given false", {"clock_skew_seconds": False}),
    ("AuthConfig", "a timeout given a whole number", {"webhook_idp_timeout_s": 3}),
    ("AuthConfig", "a timeout given false", {"webhook_idp_timeout_s": False}),
    ("AuthConfig", "a timeout that is not a number", {"webhook_idp_timeout_s": "soon"}),
    ("AuthConfig", "a list of algorithms", {"jwt_algorithms": ["RS256", "ES256"]}),
    ("AuthConfig", "a list given a bare string", {"jwt_algorithms": "RS256"}),
    ("AuthConfig", "a list with a number in it", {"jwt_algorithms": ["RS256", 256]}),
    ("AuthConfig", "a trusted proxy list", {"trusted_proxy_ips": ["10.0.0.1"]}),
    ("AuthConfig", "a field nobody defined", {"jwt_algorithm": ["RS256"]}),
    ("AuthConfig", "a clock skew written as a fractional string", {"clock_skew_seconds": "30.5"}),
    ("AuthConfig", "a clock skew written with spaces around it", {"clock_skew_seconds": " 30 "}),
    ("AuthConfig", "a clock skew given null", {"clock_skew_seconds": None}),
    ("AuthConfig", "a timeout given a boolean", {"webhook_idp_timeout_s": True}),
    ("AuthConfig", "a timeout written as a string", {"webhook_idp_timeout_s": "2.5"}),
    ("AuthConfig", "a string field given null", {"jwt_issuer": None}),
    ("AuthConfig", "an optional string given a number", {"jwt_jwks_url": 5}),
    ("AuthConfig", "a list given null", {"jwt_algorithms": None}),
    (
        "AuthConfig",
        "a bad field alongside an unsatisfiable combination",
        {"identity_provider": "webhook", "webhook_idp_require_signed_response": True, "clock_skew_seconds": "x"},
    ),
    ("TunnelConfig", "the default TTL", {}),
    ("TunnelConfig", "a TTL below the floor", {"token_ttl_s": 59}),
    ("TunnelConfig", "a TTL at the floor", {"token_ttl_s": 60}),
    ("TunnelConfig", "a TTL of zero", {"token_ttl_s": 0}),
    ("TunnelConfig", "a negative TTL", {"token_ttl_s": -1}),
    ("TunnelConfig", "a transport outside the set", {"token_transport": "header"}),
    ("TunnelConfig", "a samesite outside the set", {"cookie_samesite": "Lax"}),
    ("TunnelConfig", "a samesite the set has", {"cookie_samesite": "none"}),
    ("TunnelConfig", "a low TTL and a bad transport at once", {"token_ttl_s": 10, "token_transport": "header"}),
    ("TunnelConfig", "a TTL that is not a number at all", {"token_ttl_s": "an hour"}),
    ("ControlPlaneConfig", "a backend outside the set", {"backend": "postgres"}),
    ("ControlPlaneConfig", "a sqlite backend with nowhere to store", {"backend": "sqlite"}),
    (
        "ControlPlaneConfig",
        "a sqlite backend with somewhere to store",
        {"backend": "sqlite", "database_url": "sqlite:///x.db"},
    ),
    ("ControlPlaneConfig", "a reap interval of zero", {"reap_interval_s": 0}),
    ("ControlPlaneConfig", "a negative reap retention", {"reap_retention_s": -1}),
    ("ControlPlaneConfig", "a reap retention of zero", {"reap_retention_s": 0}),
    ("RecordingConfig", "a store outside the set", {"store_type": "s3"}),
    ("RecordingConfig", "a control-channel mode outside the set", {"control_channel_mode": "include"}),
    ("RecordingConfig", "the wire control-channel mode", {"control_channel_mode": "wire"}),
    ("RecordingConfig", "a negative size bound", {"max_bytes": -1}),
    ("RecordingConfig", "a negative retention", {"retention_s": -1}),
    ("RecordingConfig", "a directory given as a string", {"directory": "/var/rec"}),
    ("RecordingConfig", "a directory given a number", {"directory": 5}),
    ("RecordingConfig", "a directory with a trailing slash", {"directory": "/var/rec/"}),
    ("RecordingConfig", "a directory given as empty", {"directory": ""}),
    ("RecordingConfig", "a relative directory", {"directory": "rec/../rec"}),
    ("RecordingConfig", "a directory written from here", {"directory": "./rec"}),
    ("RecordingConfig", "a directory given null", {"directory": None}),
    ("RecordingConfig", "a batch size that is not a number", {"flush_batch_size": "many"}),
    ("UiConfig", "a mount path with no leading slash", {"app_path": "app"}),
    ("UiConfig", "a mount path with a trailing slash", {"app_path": "/app/"}),
    ("UiConfig", "a mount path of only slashes", {"assets_path": "///"}),
    ("UiConfig", "an empty mount path", {"app_path": ""}),
    ("UiConfig", "a mount path that is not a string", {"app_path": 5}),
    ("UiConfig", "a field nobody defined", {"xterm_cdn_sri": "sha384-x"}),
    ("ServerBindConfig", "the derived public base URL", {}),
    ("ServerBindConfig", "an explicit public base URL", {"public_base_url": "https://uterm.example"}),
    ("ServerBindConfig", "a port written as a string", {"port": "8080"}),
    ("ServerBindConfig", "a port that is not a number", {"port": "eighty"}),
    ("ServerBindConfig", "a session cap given as null", {"max_sessions": None}),
    ("ServerBindConfig", "a session cap given a number", {"max_sessions": 10}),
    ("ServerBindConfig", "an origin list", {"allowed_origins": ["https://a.example"]}),
    ("GovernanceConfig", "a cleartext policy webhook", {"policy_webhook_url": "http://policy.example/hook"}),
    ("GovernanceConfig", "a loopback policy webhook", {"policy_webhook_url": "http://127.0.0.1:9000/hook"}),
    ("GovernanceConfig", "a cleartext registry webhook", {"registry_webhook_url": "http://registry.example"}),
    ("GovernanceConfig", "a cleartext authz webhook", {"authz_webhook_url": "http://authz.example"}),
    ("GovernanceConfig", "a cleartext behavioural audit sink", {"behavioral_audit_url": "http://audit.example"}),
    ("GovernanceConfig", "a cleartext telemetry webhook", {"telemetry_webhook_url": "http://telemetry.example"}),
    ("GovernanceConfig", "a secure telemetry webhook", {"telemetry_webhook_url": "https://telemetry.example"}),
    ("GovernanceConfig", "a webhook with no scheme at all", {"policy_webhook_url": "policy.example/hook"}),
    ("GovernanceConfig", "a keystroke ceiling given as null", {"behavioral_max_cps": None}),
    ("GovernanceConfig", "a keystroke ceiling given a number", {"behavioral_max_cps": 40}),
    ("GovernanceConfig", "a keystroke ceiling given a list", {"behavioral_max_cps": []}),
    ("AuditConfig", "a field nobody defined", {"chain_path": "/var/log/chain.jsonl"}),
    ("WebhooksConfig", "loopback destinations allowed", {"allow_loopback_destinations": True}),
    ("WebhooksConfig", "a field nobody defined", {"allow_loopback": True}),
    ("ProfileStoreConfig", "a directory given as a string", {"directory": "/srv/profiles"}),
    ("PamConfig", "a mode outside the set", {"mode": "record"}),
    ("PamConfig", "the capture mode", {"mode": "capture"}),
    ("PamConfig", "a peer uid list", {"require_peer_uids": [0, 1000]}),
    ("PamConfig", "a peer uid list with a string in it", {"require_peer_uids": ["root"]}),
    ("PamConfig", "no peer uid list at all", {"require_peer_uids": None}),
    ("GraphicalTargetConfig", "a seeded target", {"target_id": "vm-1", "target_address": "10.0.0.5:5900"}),
    ("GraphicalTargetConfig", "protocol-specific parameters", {"config": {"vm_name": "win11"}}),
    ("GraphicalTargetConfig", "parameters given a list", {"config": []}),
    ("GraphicalTargetConfig", "parameters given null", {"config": None}),
    ("GraphicalTargetConfig", "a size that is not a number", {"width": "wide"}),
]

TOP_LEVEL_CASES: list[tuple[str, dict[str, Any]]] = [
    ("a worker cap of zero", {"max_workers": 0}),
    ("a negative worker cap", {"max_workers": -1}),
    ("a worker cap of one", {"max_workers": 1}),
    ("an environment outside the set", {"environment": "staging"}),
    ("the dev environment, named", {"environment": "dev"}),
    ("a frame policy outside the set", {"worker_frame_on_invalid": "close"}),
    ("the reject frame policy", {"worker_frame_on_invalid": "reject"}),
    ("a browser rate limit given a whole number", {"browser_rate_limit_per_sec": 10}),
    ("a field nobody defined", {"max_worker": 10}),
    ("a section given a scalar", {"auth": "jwt"}),
    ("a section given null", {"auth": None}),
    ("a bad value inside a section", {"auth": {"identity_provider": "ldap"}}),
    ("a name nobody defined inside a section", {"security": {"mdoe": "dev"}}),
    ("an unsatisfiable combination inside a section", {"audit": {"chain_enabled": True}}),
    ("a cleartext URL inside a section", {"pam": {"relay_url": "http://relay.example"}}),
    ("a section that is fine", {"tunnel": {"token_ttl_s": 120}}),
    ("a bad value two sections deep", {"graphical_targets": [{"width": "wide"}]}),
    ("a list of sections given one section", {"graphical_targets": {"target_id": "vm-1"}}),
    ("a session definition", {"sessions": [{"session_id": "s1"}]}),
]


def main() -> None:
    corpus = {
        "specs": {name: _spec(model) for name, model in MODELS.items()},
        "top_level_spec": _spec(schema.UtermServerConfig),
        "cases": [
            {"model": model, "name": name, "kwargs": kwargs, **_outcome(MODELS[model], kwargs)}
            for model, name, kwargs in CASES
        ],
        "top_level_cases": [
            {"name": name, "kwargs": kwargs, **_outcome(schema.UtermServerConfig, kwargs)}
            for name, kwargs in TOP_LEVEL_CASES
        ],
    }
    # NOT sorted: a section's fields are recorded in the order the model
    # declares them, and that order is what the reference reports errors in.
    # Sorting here would record a different report than the one an operator
    # reads.
    OUT.write_text(json.dumps(corpus, indent=2) + "\n")
    print(f"wrote {OUT} ({len(corpus['cases'])} cases)")


if __name__ == "__main__":
    main()
