#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the DeckMux identity bridge.

The SSH gateway sends an ``identity`` frame as the first message on a
connection whose public key was accepted, and a DeckMux hub turns it into a
participant. Everything in that frame arrives from another process, so this is
a trust boundary and the corpus is mostly about what it refuses.

**The signature is over a canonical string, not over the frame.** Fields are
joined as ``version:subject:fingerprint:transport:claims``, with the claims
serialised key-sorted and separator-free. Any port that assembles that string
differently — a different order, a different claims encoding, a spare space —
computes a different HMAC and rejects every frame the reference accepts.

**Unsigned mode is a deployment choice, not an accident.** With no expected
secret the frame is taken at its word; the module's own docstring puts that
decision on the caller. An *empty* secret is falsy and therefore also means
unsigned, which is worth pinning: a configuration that reads a missing
environment variable into an empty string does not fail closed.

**Unknown versions are ignored rather than rejected loudly**, so a newer proxy
does not break an older hub, and malformed claims are downgraded to empty
rather than losing the subject with them.

One case is recorded but deliberately *not* matched. Python's ``True in {1}``
is true, so a frame carrying ``"version": true`` is read as version 1 here.
Go's ``identityVersion`` accepts only the int and float forms and rejects a
bool, so the ports already disagree; the TypeScript port follows Go. The
Python answer is recorded below as ``python_boolean_version`` so the
divergence is visible rather than silently absorbed.

**Fallbacks are deterministic.** A subject with no display claim becomes the
part after the colon, and a connection with nothing at all still gets a
generated name and colour — a participant with no name cannot be rendered.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_deckmux_identity_golden.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from provide.uterm.deckmux._identity import identity_as_principal, parse_identity_frame, presence_from_identity

OUT = Path(__file__).with_name("deckmux_identity_golden.json")

# A corpus fixture, not a credential: the recorded signatures are only
# meaningful against this exact string.
SECRET = "s3cret-shared-with-the-gateway"  # noqa: S105


def _canonical(version: Any, subject: str, fingerprint: str, transport: str, claims: dict[str, Any]) -> str:
    """The string the signature covers."""
    claims_str = json.dumps(claims, sort_keys=True, separators=(",", ":"))
    return f"{version}:{subject}:{fingerprint}:{transport}:{claims_str}"


def _sign(canonical: str, secret: str = SECRET) -> str:
    """The HMAC a well-formed gateway would attach."""
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _signed(frame: dict[str, Any], secret: str = SECRET) -> dict[str, Any]:
    """A copy of *frame* carrying a correct signature."""
    canonical = _canonical(
        frame.get("version"),
        frame.get("subject", ""),
        frame.get("fingerprint", "") or "",
        frame.get("transport", "") or "",
        frame.get("claims") or {},
    )
    return {**frame, "signature": _sign(canonical, secret)}


BASE = {
    "type": "identity",
    "version": 1,
    "subject": "sre:alice",
    "claims": {"display_name": "Alice", "role": "operator"},
    "fingerprint": "SHA256:abc",
    "transport": "ssh",
}

# (name, frame) — parsed without a secret, so shape alone decides.
UNSIGNED_CASES: list[tuple[str, Any]] = [
    ("a well-formed frame", BASE),
    ("the wrong message type", {**BASE, "type": "presence_update"}),
    ("no type at all", {k: v for k, v in BASE.items() if k != "type"}),
    ("an unknown version", {**BASE, "version": 2}),
    ("no version", {k: v for k, v in BASE.items() if k != "version"}),
    ("a version as a string", {**BASE, "version": "1"}),
    ("a version as a float", {**BASE, "version": 1.0}),
    ("a non-integral version", {**BASE, "version": 1.5}),
    ("no subject", {k: v for k, v in BASE.items() if k != "subject"}),
    ("an empty subject", {**BASE, "subject": ""}),
    ("a subject that is not a string", {**BASE, "subject": 42}),
    ("a null subject", {**BASE, "subject": None}),
    ("no claims", {k: v for k, v in BASE.items() if k != "claims"}),
    ("claims that are not a mapping", {**BASE, "claims": ["display_name", "Alice"]}),
    ("null claims", {**BASE, "claims": None}),
    ("empty claims", {**BASE, "claims": {}}),
    ("no fingerprint", {k: v for k, v in BASE.items() if k != "fingerprint"}),
    ("a fingerprint that is not a string", {**BASE, "fingerprint": 7}),
    ("a null fingerprint", {**BASE, "fingerprint": None}),
    ("extra fields", {**BASE, "nonsense": True, "signature": "unchecked"}),
    ("a subject with no realm", {**BASE, "subject": "alice"}),
    # Two colons: the realm is split at the first, so the rest — colon and all
    # — is the name.
    ("a subject with two colons", {**BASE, "subject": "sre:team:alice"}),
]

# (name, frame, secret) — parsed against a secret, so the signature decides.
SIGNED_CASES: list[tuple[str, Any, str]] = [
    ("a correctly signed frame", _signed(BASE), SECRET),
    ("no signature", BASE, SECRET),
    ("an empty signature", {**BASE, "signature": ""}, SECRET),
    ("a signature that is not a string", {**BASE, "signature": 12345}, SECRET),
    ("a null signature", {**BASE, "signature": None}, SECRET),
    ("a signature from the wrong secret", _signed(BASE, "not-the-secret"), SECRET),
    # Shorter and longer than a SHA-256 hex digest. A comparison that reads a
    # length mismatch as a match accepts anything at all.
    ("a signature that is too short", {**BASE, "signature": "deadbeef"}, SECRET),
    (
        "a signature that is too long",
        {**BASE, "signature": _sign(_canonical(1, "sre:alice", "SHA256:abc", "ssh", BASE["claims"])) + "00"},
        SECRET,
    ),
    ("the right signature, the wrong secret", _signed(BASE), "not-the-secret"),
    # Each field is in the canonical string, so tampering with any one of them
    # after signing must break the check.
    ("the subject tampered with", {**_signed(BASE), "subject": "sre:mallory"}, SECRET),
    ("the fingerprint tampered with", {**_signed(BASE), "fingerprint": "SHA256:other"}, SECRET),
    ("the transport tampered with", {**_signed(BASE), "transport": "websocket"}, SECRET),
    ("the claims tampered with", {**_signed(BASE), "claims": {"role": "admin"}}, SECRET),
    ("the version tampered with", {**_signed(BASE), "version": 2}, SECRET),
    # The canonical form fixes the claims encoding, so a frame signed over a
    # differently-ordered mapping still verifies — the serialiser sorts.
    ("claims in a different order", _signed({**BASE, "claims": {"role": "operator", "display_name": "Alice"}}), SECRET),
    ("no transport in the frame", _signed({k: v for k, v in BASE.items() if k != "transport"}), SECRET),
    ("a transport that is not a string", _signed({**BASE, "transport": 9}), SECRET),
    ("empty claims signed", _signed({**BASE, "claims": {}}), SECRET),
    ("no fingerprint signed", _signed({k: v for k, v in BASE.items() if k != "fingerprint"}), SECRET),
    # An empty secret is falsy, so it turns verification off rather than
    # failing closed. Pinned because a config that reads a missing environment
    # variable into an empty string lands exactly here.
    ("an empty secret skips the check", {**BASE, "signature": "nonsense"}, ""),
]

# (name, claims, subject, role) — how a presence record is filled in.
PRESENCE_CASES: list[tuple[str, dict[str, Any], str, str]] = [
    ("a display name claim", {"display_name": "Alice A"}, "sre:alice", "viewer"),
    ("a display claim", {"display": "Alice B"}, "sre:alice", "viewer"),
    ("both, the first wins", {"display_name": "First", "display": "Second"}, "sre:alice", "viewer"),
    ("no display claim at all", {}, "sre:alice", "viewer"),
    ("no realm in the subject", {}, "alice", "viewer"),
    ("a subject that is only a realm", {}, "sre:", "viewer"),
    ("a subject that is only a colon", {}, ":", "viewer"),
    ("a subject with two colons", {}, "sre:team:alice", "viewer"),
    ("a subject ending in a colon", {}, "alice:", "viewer"),
    ("a whitespace subject", {}, "   ", "viewer"),
    ("a colour claim", {"color": "#123456"}, "sre:alice", "viewer"),
    ("a role claim beats the argument", {"role": "admin"}, "sre:alice", "viewer"),
    ("no role claim uses the argument", {}, "sre:alice", "operator"),
    ("no role anywhere", {}, "sre:alice", ""),
    ("a blank display claim falls through", {"display_name": "   "}, "sre:alice", "viewer"),
    ("a display claim that is not a string", {"display_name": 5}, "sre:alice", "viewer"),
    ("a colour claim that is not a string", {"color": 5}, "sre:alice", "viewer"),
    ("a role claim that is not a string", {"role": 5}, "sre:alice", "viewer"),
    ("a display claim with padding", {"display_name": "  Alice  "}, "sre:alice", "viewer"),
]


def _record_parse(name: str, frame: Any, secret: str | None) -> dict[str, Any]:
    """What the parser makes of one frame."""
    identity = parse_identity_frame(frame, secret)
    return {
        "name": name,
        "frame": frame,
        "secret": secret,
        "accepted": identity is not None,
        "subject": identity.subject if identity else None,
        "claims": identity.claims if identity else None,
        "fingerprint": identity.fingerprint if identity else None,
    }


def _record_presence(name: str, claims: dict[str, Any], subject: str, role: str) -> dict[str, Any]:
    """The participant an identity turns into."""
    identity = parse_identity_frame({"type": "identity", "version": 1, "subject": subject, "claims": claims})
    assert identity is not None, name
    presence = presence_from_identity(identity, "conn-7", role=role)
    principal = identity_as_principal(identity)
    return {
        "name": name,
        "subject": subject,
        "claims": claims,
        "role_argument": role,
        "presence": presence.to_dict(),
        "principal_subject_id": principal.subject_id,
        "principal_display_name": principal.display_name,
    }


def _record_taken_colors() -> list[dict[str, Any]]:
    """The colour walk still applies when the claim does not supply one."""
    identity = parse_identity_frame({"type": "identity", "version": 1, "subject": "sre:alice", "claims": {}})
    assert identity is not None
    natural = presence_from_identity(identity, "conn-7").color
    return [
        {"name": "nothing taken", "taken": [], "color": natural},
        {
            "name": "its natural colour taken",
            "taken": [natural],
            "color": presence_from_identity(identity, "conn-7", taken_colors=frozenset({natural})).color,
        },
        {
            "name": "a claimed colour ignores what is taken",
            "taken": ["#123456"],
            "color": presence_from_identity(
                parse_identity_frame(
                    {"type": "identity", "version": 1, "subject": "sre:alice", "claims": {"color": "#123456"}}
                ),  # type: ignore[arg-type]
                "conn-7",
                taken_colors=frozenset({"#123456"}),
            ).color,
        },
    ]


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "secret": SECRET,
        "canonical_example": _canonical(1, "sre:alice", "SHA256:abc", "ssh", BASE["claims"]),  # type: ignore[arg-type]
        # Recorded, not matched: see the module docstring. Python reads a bool
        # as an int, Go does not, and the TypeScript port follows Go.
        "python_boolean_version": _record_parse("a boolean version", {**BASE, "version": True}, None)["accepted"],
        "unsigned": [_record_parse(name, frame, None) for name, frame in UNSIGNED_CASES],
        "signed": [_record_parse(name, frame, secret) for name, frame, secret in SIGNED_CASES],
        # Signed over claims in a non-sorted insertion order. Recorded as the
        # signature alone because the corpus file is itself key-sorted, so the
        # frame cannot carry the order that makes the point — the test rebuilds
        # it.
        "unsorted_claims_signature": _signed({**BASE, "claims": {"role": "operator", "display_name": "Alice"}})[
            "signature"
        ],
        "presence": [_record_presence(*case) for case in PRESENCE_CASES],
        "taken_colors": _record_taken_colors(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(UNSIGNED_CASES)} unsigned, {len(SIGNED_CASES)} signed, {len(PRESENCE_CASES)} presence)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
