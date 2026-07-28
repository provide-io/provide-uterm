#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript server-auth port.

Three primitives, each of which decides whether somebody gets in.

**Webhook signatures.** Signed over ``"{timestamp}.{body}"`` so a captured
request cannot be replayed later, and verified fail-closed when there is no
signing secret — an empty key HMACs to something any attacker who knows the
body and timestamp can forge, so the check has to refuse *before* it touches
the signature.

**The role allow-list.** Roles arrive from a JWT, a proxy header or a webhook
IDP, all of which are outside this server's control. Anything that is not one
of the three canonical roles is dropped, so a compromised issuer cannot mint
``superuser``; and when filtering leaves nothing, the fallback is the *least*
privileged role rather than an empty set that some caller might read as "no
restrictions".

**API keys and tenants.** The raw key is never stored — only its digest — and
a tenant id is a bounded ASCII slug shared verbatim with the Go and C# ports,
so the same tenant validates identically on every surface. A key belonging to
one tenant must not be listable or revocable by another.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_serverauth_golden.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from provide.uterm.server.api_keys import ApiKeyStore, canonical_tenant_id
from provide.uterm.server.auth_roles import _DEFAULT_ROLE, _KNOWN_ROLES, _filter_known_roles
from provide.uterm.server.webhook_signing import _DEFAULT_MAX_AGE_S, build_webhook_signature, verify_webhook_signature

OUT = Path(__file__).with_name("serverauth_golden.json")

SECRET = "s3cr3t-signing-key"  # noqa: S105  # a fixture, not a credential
TIMESTAMP = "1700000000"
NOW = 1700000000.0

# (name, secret, body) — what a signature is taken over.
SIGNATURE_CASES: list[tuple[str, str, bytes]] = [
    ("a json body", SECRET, b'{"decision":"allow"}'),
    ("an empty body", SECRET, b""),
    ("a body with a dot in it", SECRET, b"1700000000.not-the-real-body"),
    ("a body with high bytes", SECRET, bytes(range(250, 256))),
    ("a different secret", "another-key", b'{"decision":"allow"}'),
]

# (name, roles) — what an external issuer may claim.
ROLE_CASES: list[tuple[str, Any]] = [
    ("nothing at all", []),
    ("one known role", ["operator"]),
    ("every known role", ["viewer", "operator", "admin"]),
    ("an unknown role", ["superuser"]),
    ("a known and an unknown role", ["operator", "root"]),
    ("mixed case", ["OPERATOR", "Admin"]),
    ("padded with whitespace", ["  operator  "]),
    ("empty strings", ["", "   "]),
    ("duplicates", ["operator", "operator", "OPERATOR"]),
    ("something that is not a string", [1, None, True]),
    ("a string rather than a list", "operator"),
    ("a mapping, which iterates its keys", {"operator": 1, "root": 2}),
    ("a tuple", ("admin",)),
    ("a set", frozenset({"viewer"})),
]

# (name, value) — values that are not iterable at all. The reference reaches
# for an iterator and raises; a port that quietly returned the default role
# would hide the caller's bug and grant access on a type error.
ROLE_FAILURE_CASES: list[tuple[str, Any]] = [
    ("a number", 42),
    ("none", None),
    ("a boolean", True),
]

# (name, tenant) — the shared tenant slug.
TENANT_CASES: list[tuple[str, Any]] = [
    ("a simple slug", "acme"),
    ("with separators", "acme-corp.eu_1"),
    ("starting with a digit", "1acme"),
    ("padded", "  acme  "),
    ("empty", ""),
    ("whitespace only", "   "),
    ("none", None),
    ("starting with a separator", "-acme"),
    ("starting with a dot", ".acme"),
    ("with a slash", "acme/evil"),
    ("with a space inside", "acme corp"),
    ("with a null byte", "acme\x00"),
    ("at the length limit", "a" * 128),
    ("one past the limit", "a" * 129),
    ("unicode", "acmé"),
]


def _describe_signature(secret: str, body: bytes) -> dict[str, Any]:
    """Record the signature and whether it verifies."""
    signature = build_webhook_signature(secret, body, TIMESTAMP)
    return {
        "signature": signature,
        "verifies": verify_webhook_signature(SECRET, body, signature, TIMESTAMP, now=NOW),
    }


def _ambiguity_case() -> dict[str, Any]:
    """Record the one place the signed material is ambiguous.

    The material is ``timestamp + "." + body``, so a body that begins with
    digits and a dot can be re-read as part of the timestamp: a signature over
    ``0.body`` at ``17000000`` is the same string as one over ``body`` at
    ``17000000.0``, and both timestamps name the same instant, so the second
    passes verification. Recorded rather than fixed — the scheme is shared
    with the Go and C# ports, and changing it here alone would break them.
    """
    early = build_webhook_signature(SECRET, b"0.body", "17000000")
    late = build_webhook_signature(SECRET, b"body", "17000000.0")
    return {
        "signatures_collide": early == late,
        "cross_verifies": verify_webhook_signature(SECRET, b"body", early, "17000000.0", now=17000000.0),
    }


def _verification_cases() -> dict[str, Any]:
    """Record every way verification can fail."""
    body = b'{"decision":"allow"}'
    good = build_webhook_signature(SECRET, body, TIMESTAMP)
    return {
        "accepts a good signature": verify_webhook_signature(SECRET, body, good, TIMESTAMP, now=NOW),
        "accepts it without the prefix": verify_webhook_signature(
            SECRET, body, good.split("=", 1)[1], TIMESTAMP, now=NOW
        ),
        "accepts a mixed-case prefix": verify_webhook_signature(
            SECRET, body, "SHA256=" + good.split("=", 1)[1], TIMESTAMP, now=NOW
        ),
        "accepts a padded signature": verify_webhook_signature(SECRET, body, f"  {good}  ", TIMESTAMP, now=NOW),
        "refuses no secret": verify_webhook_signature(None, body, good, TIMESTAMP, now=NOW),
        "refuses an empty secret": verify_webhook_signature("", body, good, TIMESTAMP, now=NOW),
        "refuses a whitespace secret": verify_webhook_signature("   ", body, good, TIMESTAMP, now=NOW),
        "refuses no signature": verify_webhook_signature(SECRET, body, None, TIMESTAMP, now=NOW),
        "refuses an empty signature": verify_webhook_signature(SECRET, body, "", TIMESTAMP, now=NOW),
        "refuses a bare prefix": verify_webhook_signature(SECRET, body, "sha256=", TIMESTAMP, now=NOW),
        "refuses no timestamp": verify_webhook_signature(SECRET, body, good, None, now=NOW),
        "refuses a timestamp that is not a number": verify_webhook_signature(SECRET, body, good, "soon", now=NOW),
        # A whitespace timestamp parses as zero in some languages, which would
        # be a timestamp from 1970 and so outside every window — but relying
        # on that is relying on an accident.
        "refuses a whitespace timestamp": verify_webhook_signature(SECRET, body, good, "   ", now=NOW),
        "refuses a changed body": verify_webhook_signature(SECRET, b'{"decision":"deny"}', good, TIMESTAMP, now=NOW),
        "refuses a wrong secret": verify_webhook_signature("other", body, good, TIMESTAMP, now=NOW),
        "refuses a stale timestamp": verify_webhook_signature(
            SECRET, body, good, TIMESTAMP, now=NOW + _DEFAULT_MAX_AGE_S + 1
        ),
        "refuses a timestamp from the future": verify_webhook_signature(
            SECRET, body, good, TIMESTAMP, now=NOW - _DEFAULT_MAX_AGE_S - 1
        ),
        "accepts one at the edge of the window": verify_webhook_signature(
            SECRET, body, good, TIMESTAMP, now=NOW + _DEFAULT_MAX_AGE_S
        ),
        # A signature actually made with a whitespace secret. Refused because
        # there is no secret, not because the digests happen to differ.
        "refuses a signature made with a whitespace secret": verify_webhook_signature(
            "   ", body, build_webhook_signature("   ", body, TIMESTAMP), TIMESTAMP, now=NOW
        ),
        # A signature actually made with an unparseable timestamp, so the
        # digests would match if the guard were not there.
        "refuses a signature made with a timestamp that is not a number": verify_webhook_signature(
            SECRET, body, build_webhook_signature(SECRET, body, "soon"), "soon", now=NOW
        ),
        # A padded header: the signature is taken over the header as sent, so
        # one made from the trimmed form does not verify.
        "refuses a padded timestamp header": verify_webhook_signature(
            SECRET, body, build_webhook_signature(SECRET, body, TIMESTAMP), f" {TIMESTAMP} ", now=NOW
        ),
        # A signature of the wrong length entirely.
        "refuses a short signature": verify_webhook_signature(SECRET, body, "sha256=abc", TIMESTAMP, now=NOW),
        "refuses a signature for another timestamp": verify_webhook_signature(
            SECRET, body, build_webhook_signature(SECRET, body, "1699999999"), TIMESTAMP, now=NOW
        ),
    }


def _role_failure(value: Any) -> str | None:
    """Name whatever escapes when roles are not iterable."""
    try:
        _filter_known_roles(value)
    except TypeError as exc:
        return type(exc).__name__
    return None


def _api_key_cases() -> dict[str, Any]:
    """Drive the key store through creation, validation and revocation."""
    store = ApiKeyStore()
    raw, record = store.create("ci", scopes=frozenset({"operator"}))
    other_raw, _other = store.create("laptop")

    results: dict[str, Any] = {
        "raw_key_length": len(raw),
        "key_id_length": len(record.key_id),
        "key_id_is_hash_prefix": record.key_hash.startswith(record.key_id),
        "hash_length": len(record.key_hash),
        "raw_key_is_not_stored": raw not in (record.key_hash, record.key_id),
        "keys_are_distinct": raw != other_raw,
        "validates_its_own_key": store.validate(raw) is not None,
        "records_last_used": store.validate(raw).last_used_at is not None,  # type: ignore[union-attr]
        "refuses an unknown key": store.validate("not-a-key") is None,
        "default_tenant_is_empty": record.tenant_id,
        "default_scopes_empty": sorted(_other.scopes),
        "scopes_kept": sorted(record.scopes),
    }

    revoked_raw, revoked = store.create("dead")
    results["revoke_reports_found"] = store.revoke(revoked.key_id)
    results["revoke_reports_unknown"] = store.revoke("nope")
    results["revoked_key_is_refused"] = store.validate(revoked_raw) is None

    expired_raw, _expired = store.create("expired", expires_in_s=-1)
    results["expired_key_is_refused"] = store.validate(expired_raw) is None
    living_raw, living = store.create("living", expires_in_s=3600)
    results["living_key_is_accepted"] = store.validate(living_raw) is not None
    results["expiry_is_in_the_future"] = living.expires_at is not None and living.expires_at > time.time()
    results["no_expiry_by_default"] = record.expires_at is None

    tenant_store = ApiKeyStore()
    _acme_raw, acme = tenant_store.create_for_tenant("acme", "ci")
    _other_raw, _other_tenant = tenant_store.create_for_tenant("  globex  ", "ci")
    results["tenant_is_canonical"] = acme.tenant_id
    results["padded_tenant_is_trimmed"] = _other_tenant.tenant_id
    results["invalid_tenant_refused"] = _tenant_failure(tenant_store, "-bad")
    results["empty_tenant_refused"] = _tenant_failure(tenant_store, "")
    results["lists_only_its_own"] = [key.name for key in tenant_store.list_keys_for_tenant("acme")]
    results["lists_nothing_for_an_invalid_tenant"] = tenant_store.list_keys_for_tenant("-bad")
    results["lists_nothing_for_an_unknown_tenant"] = tenant_store.list_keys_for_tenant("nobody")
    results["revokes_only_its_own"] = tenant_store.revoke_for_tenant(acme.key_id, "globex")
    results["revokes_its_own"] = tenant_store.revoke_for_tenant(acme.key_id, "acme")
    results["revoked_key_leaves_the_listing"] = [key.name for key in tenant_store.list_keys_for_tenant("acme")]
    results["revoke_for_an_invalid_tenant"] = tenant_store.revoke_for_tenant(acme.key_id, "-bad")
    results["revoke_an_unknown_key"] = tenant_store.revoke_for_tenant("nope", "acme")
    # A flat key has an empty tenant. An invalid tenant id must not be treated
    # as "the empty tenant" and so gain the right to revoke it.
    _flat_raw, flat = tenant_store.create("flat")
    results["invalid_tenant_cannot_revoke_a_flat_key"] = tenant_store.revoke_for_tenant(flat.key_id, "-bad")
    results["empty_tenant_cannot_revoke_a_flat_key"] = tenant_store.revoke_for_tenant(flat.key_id, "")
    results["list_keys_counts_everything"] = len(tenant_store.list_keys())
    return results


def _tenant_failure(store: ApiKeyStore, tenant: str) -> str | None:
    """Record the refusal a bad tenant id produces."""
    try:
        store.create_for_tenant(tenant, "ci")
    except ValueError as exc:
        return str(exc)
    return None


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "timestamp": TIMESTAMP,
        "now": NOW,
        "secret": SECRET,
        "max_age_s": _DEFAULT_MAX_AGE_S,
        "signatures": [
            {"name": name, "secret": secret, "body": list(body), **_describe_signature(secret, body)}
            for name, secret, body in SIGNATURE_CASES
        ],
        "verification": _verification_cases(),
        "ambiguity": _ambiguity_case(),
        "roles": [
            {
                "name": name,
                "input": sorted(value) if isinstance(value, (frozenset, set, tuple)) else value,
                "resolved": sorted(_filter_known_roles(value)),
            }
            for name, value in ROLE_CASES
        ],
        "role_failures": [{"name": name, "error": _role_failure(value)} for name, value in ROLE_FAILURE_CASES],
        "known_roles": sorted(_KNOWN_ROLES),
        "default_role": _DEFAULT_ROLE,
        "tenants": [
            {"name": name, "input": value, "canonical": canonical_tenant_id(value)} for name, value in TENANT_CASES
        ],
        "api_keys": _api_key_cases(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(SIGNATURE_CASES)} signatures, {len(ROLE_CASES)} roles, {len(TENANT_CASES)} tenants)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
