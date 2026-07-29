#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for tunnel invites and token hashing.

A tunnel invite is what somebody redeems to get a share or control token for
a session they were handed a link to. It is the whole bootstrap, so what it
refuses matters:

* **The store holds hashes, not invites.** A memory disclosure on the server
  leaks digests. The same is true of the tunnel tokens themselves — hashed at
  issuance and compared in constant time at every authentication site, so a
  configured-but-empty slot can never authenticate anybody.
* **One use.** Redeeming removes the entry before anything about it is
  checked, so a second attempt with the same invite finds nothing — including
  when the first attempt was refused for being expired or for naming the
  wrong session. An invite that fails is spent.
* **Five minutes, or the tunnel's own life, whichever ends first.** An invite
  cannot outlive the tunnel it lets somebody into.
* **The session is named by the caller and checked against the invite**, so
  an invite for one session cannot be redeemed against another.

The hashes are recorded for their exact bytes: the port uses BLAKE2b-256
from a library rather than Node's `createHash`, which offers only
BLAKE2b-512 — and truncating that would *not* give the same digest, since
the output length is mixed into BLAKE2b's parameter block.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_tunnelinvites_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.server import tunnel_invites as ti
from provide.uterm.tunnel import token_hash

OUT = Path(__file__).resolve().parent / "tunnelinvites_golden.json"

# A fixed instant, so an expiry is a number in the corpus rather than "now".
NOW = 1_700_000_000.0

HASHED: list[tuple[str, str]] = [
    ("an ordinary token", "s3cret-token"),  # pragma: allowlist secret
    ("an empty token", ""),
    ("a space", " "),
    ("text outside ASCII", "héllo ☃"),
    ("a long token", "x" * 512),
    ("a token differing in one bit", "s3cret-tokeo"),  # pragma: allowlist secret
]

VERIFIED: list[tuple[str, str, str]] = [
    ("the right token", "s3cret-token", token_hash.hash_token("s3cret-token")),  # pragma: allowlist secret
    ("the wrong token", "other", token_hash.hash_token("s3cret-token")),  # pragma: allowlist secret
    ("an empty token against a real hash", "", token_hash.hash_token("s3cret-token")),  # pragma: allowlist secret
    ("a real token against an empty hash", "s3cret-token", ""),  # pragma: allowlist secret
    ("nothing against nothing", "", ""),
    (
        "a hash of the hash",
        token_hash.hash_token("s3cret-token"),
        token_hash.hash_token("s3cret-token"),
    ),  # pragma: allowlist secret
    ("the right token in capitals", "S3CRET-TOKEN", token_hash.hash_token("s3cret-token")),  # pragma: allowlist secret
    (
        "a stored hash in capitals",
        "s3cret-token",
        token_hash.hash_token("s3cret-token").upper(),
    ),  # pragma: allowlist secret
]


def _stored(**overrides: Any) -> dict[str, object]:
    """An invite as the store holds it, with fields replaced."""
    entry: dict[str, object] = {
        "session_id": "sess-1",
        "role": "viewer",
        "tunnel_token": "share-token",  # pragma: allowlist secret
        "expires_at": NOW + 60.0,
        "issued_ip": "203.0.113.7",
    }
    entry.update(overrides)
    return entry


def _without(*names: str) -> dict[str, object]:
    """An invite the store holds with fields missing outright."""
    entry = _stored()
    for name in names:
        entry.pop(name)
    return entry


CONSUMED: list[tuple[str, dict[str, object] | None, str, str, float]] = [
    ("a viewer invite", _stored(), "invite-1", "sess-1", NOW),
    ("an operator invite", _stored(role="operator", tunnel_token="control-token"), "invite-1", "sess-1", NOW),  # noqa: S106
    ("an invite nobody issued", None, "invite-1", "sess-1", NOW),
    ("an empty invite", _stored(), "", "sess-1", NOW),
    ("an invite that is only spaces", _stored(), "   ", "sess-1", NOW),
    ("an invite with spaces around it", _stored(), "  invite-1  ", "sess-1", NOW),
    ("an invite at the instant it expires", _stored(), "invite-1", "sess-1", NOW + 60.0),
    ("an invite a moment past expiry", _stored(), "invite-1", "sess-1", NOW + 60.001),
    ("an invite whose expiry is missing", _stored(expires_at=None), "invite-1", "sess-1", NOW),
    ("an invite whose expiry is not a number", _stored(expires_at="soon"), "invite-1", "sess-1", NOW),
    ("an invite whose expiry is an integer", _stored(expires_at=int(NOW) + 60), "invite-1", "sess-1", NOW),
    ("an invite whose expiry is a boolean", _stored(expires_at=True), "invite-1", "sess-1", NOW),
    ("an invite for another session", _stored(), "invite-1", "sess-2", NOW),
    ("an invite naming no session", _stored(session_id=""), "invite-1", "sess-1", NOW),
    ("an invite holding a role nobody defined", _stored(role="admin"), "invite-1", "sess-1", NOW),
    ("an invite holding a role in capitals", _stored(role="Viewer"), "invite-1", "sess-1", NOW),
    ("an invite holding no role", _stored(role=""), "invite-1", "sess-1", NOW),
    ("an invite holding no token", _stored(tunnel_token=""), "invite-1", "sess-1", NOW),
    ("an invite whose token is only spaces", _stored(tunnel_token="   "), "invite-1", "sess-1", NOW),  # noqa: S106
    ("an invite whose token has spaces around it", _stored(tunnel_token=" tok "), "invite-1", "sess-1", NOW),  # noqa: S106
    ("an invite issued to nobody in particular", _stored(issued_ip=None), "invite-1", "sess-1", NOW),
    ("an invite missing its session outright", _without("session_id"), "invite-1", "sess-1", NOW),
    ("an invite missing its role outright", _without("role"), "invite-1", "sess-1", NOW),
    ("an invite missing its token outright", _without("tunnel_token"), "invite-1", "sess-1", NOW),
    ("an invite missing its expiry outright", _without("expires_at"), "invite-1", "sess-1", NOW),
    ("an invite missing the address it was issued to", _without("issued_ip"), "invite-1", "sess-1", NOW),
    # `str(None)` is "None", not "": what the reference makes of a field
    # present and null is not what it makes of one absent.
    ("an invite whose session is null", _stored(session_id=None), "invite-1", "sess-1", NOW),
    ("an invite whose role is null", _stored(role=None), "invite-1", "sess-1", NOW),
    ("an invite whose token is null", _stored(tunnel_token=None), "invite-1", "sess-1", NOW),
]


def _consume_case(
    name: str, entry: dict[str, object] | None, offered: str, session_id: str, now: float
) -> dict[str, Any]:
    """Redeem one invite, recording what came back and what the store kept."""
    store: dict[str, dict[str, object]] = {}
    if entry is not None:
        store[token_hash.hash_token("invite-1")] = dict(entry)
    # A second entry that no case touches, so "the store was emptied" and "the
    # one entry was removed" cannot be confused.
    store[token_hash.hash_token("other-invite")] = _stored(session_id="sess-9")

    result = ti.consume_tunnel_invite(store, offered, session_id=session_id, now=now)
    # Offered again: an invite is spent whether or not redeeming it worked.
    again = ti.consume_tunnel_invite(store, offered, session_id=session_id, now=now)
    return {
        "name": name,
        "stored": entry,
        "offered": offered,
        "session_id": session_id,
        "now": now,
        "consumed": None
        if result is None
        else {
            "session_id": result.session_id,
            "role": result.role,
            "tunnel_token": result.tunnel_token,
            "expires_at": result.expires_at,
            "issued_ip": result.issued_ip,
        },
        "second_attempt_consumed": again is not None,
        "remaining_keys": sorted(store),
    }


def _issue_cases() -> list[dict[str, Any]]:
    """What issuing puts in the store, and when the invites die.

    The invites themselves are random, so what is recorded is everything
    about them that is not: how long they live, the roles and tokens they
    carry, and that the store is keyed by their hashes rather than by them.
    """
    cases: list[dict[str, Any]] = []
    for name, tunnel_expires_at in (
        ("a tunnel outliving the invite window", NOW + 3600.0),
        ("a tunnel ending inside the invite window", NOW + 30.0),
        ("a tunnel ending exactly at the window's edge", NOW + float(ti.INVITE_TTL_S)),
        ("a tunnel that has already ended", NOW - 1.0),
    ):
        store: dict[str, dict[str, object]] = {}
        share_invite, control_invite = ti.issue_tunnel_invites(
            store,
            session_id="sess-1",
            share_token="share-token",  # noqa: S106  # pragma: allowlist secret
            control_token="control-token",  # noqa: S106  # pragma: allowlist secret
            tunnel_expires_at=tunnel_expires_at,
            issued_ip="203.0.113.7",
            now=NOW,
        )
        cases.append(
            {
                "name": name,
                "tunnel_expires_at": tunnel_expires_at,
                "invite_length": len(share_invite),
                "invites_differ": share_invite != control_invite,
                "keyed_by_hash": sorted(store)
                == sorted({token_hash.hash_token(share_invite), token_hash.hash_token(control_invite)}),
                "entries": sorted(
                    (dict(value) for value in store.values()),
                    key=lambda value: str(value["role"]),
                ),
            }
        )
    return cases


def _sweep_case() -> dict[str, Any]:
    """What a sweep removes: everything past its expiry, and nothing else."""
    store: dict[str, dict[str, object]] = {
        "live": _stored(expires_at=NOW + 60.0),
        "at-the-instant": _stored(expires_at=NOW),
        "expired": _stored(expires_at=NOW - 1.0),
        "no-expiry": _stored(expires_at=None),
        "expiry-not-a-number": _stored(expires_at="soon"),
    }
    ti.sweep_expired_tunnel_invites(store, now=NOW)
    return {"remaining_keys": sorted(store)}


def _discard_case() -> dict[str, Any]:
    """What discarding a session's invites removes."""
    store: dict[str, dict[str, object]] = {
        "one": _stored(session_id="sess-1"),
        "two": _stored(session_id="sess-1", role="operator"),
        "other": _stored(session_id="sess-2"),
        "unnamed": _stored(session_id=""),
    }
    ti.discard_tunnel_invites_for_session(store, "sess-1")
    return {"remaining_keys": sorted(store)}


def main() -> None:
    corpus = {
        "invite_ttl_s": ti.INVITE_TTL_S,
        "now": NOW,
        "hashed": [{"name": name, "plain": plain, "hash": token_hash.hash_token(plain)} for name, plain in HASHED],
        "verified": [
            {"name": name, "plain": plain, "stored_hash": stored, "matches": token_hash.verify_token(plain, stored)}
            for name, plain, stored in VERIFIED
        ],
        "issued": _issue_cases(),
        "consumed": [_consume_case(*case) for case in CONSUMED],
        "swept": _sweep_case(),
        "discarded": _discard_case(),
        "matches_token_hash": [
            {
                "name": name,
                "tunnel_token": token,
                "token_hash": stored,
                "matches": ti.tunnel_invite_matches_token_hash(
                    ti.TunnelInvite(session_id="sess-1", role="viewer", tunnel_token=token, expires_at=NOW), stored
                ),
            }
            for name, token, stored in (
                (
                    "the tunnel's active token",
                    "share-token",
                    token_hash.hash_token("share-token"),
                ),  # pragma: allowlist secret
                (
                    "a token the tunnel has rotated away from",
                    "share-token",
                    token_hash.hash_token("new-token"),
                ),  # pragma: allowlist secret
                ("a tunnel with no token hash at all", "share-token", ""),  # pragma: allowlist secret
            )
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['consumed'])} redemptions)")


if __name__ == "__main__":
    main()
