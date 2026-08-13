#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the ctrlmsg builder golden from the REAL Python builders
(provide.uterm.control_channel_builders). Run from the repo root:

    uv run python packages/provide-uterm-go/ctrlmsg/testdata/gen_builder_golden.py

Writes builder_golden.json next to this script. golden_test.go asserts Go's
builders emit the same message for every case, so a divergence in either fails
CI. It does NOT touch signature_corpus.json, the 544-row HMAC corpus beside it
— that file has its own provenance and is left alone.

golden_test.go used to point at a `<scratch>/gen_ctrlmsg_golden.py` that was
never committed, which is exactly why this corpus went unchecked: the drift
check can only verify a corpus it can re-derive.

The identity signatures are the sharp edge. They HMAC a canonical-JSON payload,
so claim INSERTION ORDER is part of the input, not an incidental detail — the
dicts below are written in the order the corpus records, and reordering them
changes the signature. Three different secrets appear across the four signed
cases; they are synthetic test vectors, not credentials.
"""

from __future__ import annotations

import json
import pathlib

from provide.uterm.control_channel_builders import (
    make_identity,
    make_link_patterns,
    make_presence_update,
    make_resume,
    make_resume_failed,
    make_resume_ok,
    make_session_token,
)

PROXY_SECRET = "proxy-secret"  # noqa: S105


def build() -> dict[str, object]:
    return {
        # --- identity: unsigned, signed, and the encoding edges -------------
        "identity_default": make_identity("user:alice", transport="ssh"),
        "identity_empty_claims_signed": make_identity(
            "user:x",
            claims={},
            transport="ssh",
            secret="k",  # noqa: S106
        ),
        "identity_full": make_identity(
            "user:bob",
            claims={"org": "acme", "role": "admin"},
            fingerprint="SHA256:abc123",
            transport="ws",
        ),
        "identity_signed": make_identity(
            "user:alice",
            # scope is deliberately NOT sorted: list order is signed too.
            claims={"role": "admin", "scope": ["write", "read"]},
            fingerprint="SHA256:abc",
            transport="ws",
            secret=PROXY_SECRET,
        ),
        "identity_signed_no_claims": make_identity(
            "user:alice",
            fingerprint="fp",
            transport="ssh",
            secret=PROXY_SECRET,
        ),
        "identity_unicode_claims": make_identity(
            # Astral-free but non-ASCII on both sides: subject, a claim value,
            # and a bare arrow, so any UTF-8/escaping difference moves the HMAC.
            "üser",
            claims={"arrow": "→", "n": 3, "name": "José"},
            transport="ssh",
            secret="s",  # noqa: S106
        ),
        # --- link patterns: empty, single, multi, and every optional field --
        "link_all_optional": make_link_patterns(
            [
                {
                    "action": "url",
                    "class": "external-link",
                    "flags": "gi",
                    "group": 1,
                    "hover": "Open link",
                    "id": "p.num",
                    "pattern": r"\d+",
                    "payload": "https://example.com/",
                }
            ]
        ),
        "link_empty": make_link_patterns([]),
        "link_line_contains": make_link_patterns(
            [{"action": "cmd", "line_contains": "Warps to Sector", "pattern": r"\((\d+)\)"}]
        ),
        "link_multi": make_link_patterns(
            [
                {"action": "cmd", "pattern": "alpha"},
                {"action": "url", "pattern": "beta"},
                {"action": "key", "pattern": "gamma"},
            ]
        ),
        "link_single": make_link_patterns([{"action": "cmd", "pattern": r"\bsector\b"}]),
        # --- presence: minimal vs every optional field ----------------------
        "presence_fields": make_presence_update("u2", cursor_col=10, scroll_line=42),
        "presence_min": make_presence_update("u1"),
        # --- resume: the empty-vs-absent reason distinction matters ---------
        "resume_failed_empty": make_resume_failed(""),
        "resume_failed_none": make_resume_failed(),
        "resume_failed_reason": make_resume_failed("token expired"),
        "resume_min": make_resume("resume-tok"),
        "resume_ok": make_resume_ok(),
        "resume_player": make_resume("resume-tok", player_id=15),
        # --- session token: player_id 0 must survive a falsy check ----------
        "session_token_min": make_session_token("tok-abc"),
        "session_token_player": make_session_token("tok-xyz", player_id=42),
        "session_token_player_zero": make_session_token("tok", player_id=0),
    }


def main() -> None:
    corpus = build()
    out = pathlib.Path(__file__).with_name("builder_golden.json")
    out.write_text(json.dumps(corpus, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(corpus)} cases)")


if __name__ == "__main__":
    main()
