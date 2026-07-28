#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript auth port.

This is the boundary between *transport* — the SSH handshake and the public
key it carries — and *identity*, which the consuming application owns. Two
things have to be exact:

* **The fingerprint.** It is the whole basis of the match, and it has to be
  the same string `ssh-keygen -lf` prints: base64 of the SHA-256 of the
  *decoded* wire bytes, with the padding stripped. Fingerprint a different
  set of bytes, or leave the `=` on, and every key stops resolving — or, far
  worse, two keys collide into one identity.
* **The `authorized_keys` grammar.** The options field ends at the first
  whitespace *outside* quotes, so `command="echo hi",no-pty` is one token;
  reading it as two would treat `no-pty` as the key type. The subject falls
  back from the `subject=` option to the comment to `key:<fp>`, and a
  malformed line is skipped rather than aborting the file, because one bad
  entry must not lock everybody out.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_auth_golden.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
from pathlib import Path
from typing import Any

from provide.uterm.auth import (
    AuthorizedKeysFileResolver,
    NullResolver,
    _parse_authorized_keys_line,
    fingerprint_from_openssh_blob,
)

OUT = Path(__file__).with_name("auth_golden.json")

# Deterministic stand-ins for real key payloads. What matters is that the
# same bytes fingerprint the same way, not that they are valid curve points.
PAYLOADS = {
    "ed25519": base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + bytes(range(32))).decode(),
    "rsa": base64.b64encode(b"\x00\x00\x00\x07ssh-rsa" + bytes(range(64))).decode(),
    "ecdsa": base64.b64encode(b"\x00\x00\x00\x13ecdsa-sha2-nistp256" + bytes(range(16))).decode(),
    "sk": base64.b64encode(b"\x00\x00\x00\x22sk-ssh-ed25519@openssh.com" + bytes(range(8))).decode(),
}

# (name, blob) — every input shape the fingerprint helper is handed.
BLOB_CASES: list[tuple[str, bytes]] = [
    ("openssh text", f"ssh-ed25519 {PAYLOADS['ed25519']}".encode()),
    ("openssh text with a comment", f"ssh-ed25519 {PAYLOADS['ed25519']} alice@laptop".encode()),
    ("openssh text with trailing space", f"ssh-ed25519 {PAYLOADS['ed25519']} \n".encode()),
    # Leading whitespace too: without the strip the key type no longer starts
    # the string, and the *text* gets fingerprinted instead of the key.
    ("openssh text with leading space", f"  ssh-ed25519 {PAYLOADS['ed25519']}".encode()),
    ("rsa", f"ssh-rsa {PAYLOADS['rsa']}".encode()),
    ("ecdsa", f"ecdsa-sha2-nistp256 {PAYLOADS['ecdsa']}".encode()),
    ("security key", f"sk-ssh-ed25519@openssh.com {PAYLOADS['sk']}".encode()),
    ("binary wire format", base64.b64decode(PAYLOADS["ed25519"])),
    # Only the first token after the key type is the payload; anything after
    # it is a comment, even when it looks like more base64.
    ("a second base64 token is a comment", b"ssh-ed25519 AAAA BBBB"),
    ("empty", b""),
]

# (name, blob) — inputs the helper refuses.
BAD_BLOB_CASES: list[tuple[str, bytes]] = [
    ("keytype with no payload", b"ssh-ed25519"),
    ("keytype with only whitespace after it", b"ssh-ed25519   "),
    ("payload that is not base64", b"ssh-ed25519 not-base64!!"),
]

# (name, line) — the authorized_keys grammar.
LINE_CASES: list[tuple[str, str]] = [
    ("bare key", f"ssh-ed25519 {PAYLOADS['ed25519']}"),
    ("key with a comment", f"ssh-ed25519 {PAYLOADS['ed25519']} alice@laptop"),
    ("comment with spaces", f"ssh-ed25519 {PAYLOADS['ed25519']} alice on her laptop"),
    ("explicit subject", f'subject="sre:alice" ssh-ed25519 {PAYLOADS["ed25519"]} alice@laptop'),
    ("empty subject falls back", f'subject="" ssh-ed25519 {PAYLOADS["ed25519"]} alice@laptop'),
    ("subject with no comment", f'subject="sre:alice" ssh-ed25519 {PAYLOADS["ed25519"]}'),
    ("one claim", f'claim-role="oncall" ssh-ed25519 {PAYLOADS["ed25519"]} alice@laptop'),
    (
        "several claims",
        f'subject="sre:alice",claim-role="oncall",claim-display="alice" ssh-ed25519 {PAYLOADS["ed25519"]}',
    ),
    ("an unrecognised option", f"no-pty ssh-ed25519 {PAYLOADS['ed25519']} alice@laptop"),
    (
        "a quoted option containing a space and a comma",
        f'command="echo hi, there",no-pty ssh-ed25519 {PAYLOADS["ed25519"]} alice@laptop',
    ),
    ("options and claims together", f'no-pty,claim-role="oncall" ssh-ed25519 {PAYLOADS["ed25519"]}'),
    ("an unquoted option value", f"environment=FOO=bar ssh-ed25519 {PAYLOADS['ed25519']}"),
    ("a repeated option", f'claim-role="first",claim-role="second" ssh-ed25519 {PAYLOADS["ed25519"]}'),
    ("an empty option between commas", f'no-pty,,claim-role="oncall" ssh-ed25519 {PAYLOADS["ed25519"]}'),
    ("extra whitespace after the options", f"no-pty    ssh-ed25519 {PAYLOADS['ed25519']}"),
    ("options ending in a comma", f"no-pty, ssh-ed25519 {PAYLOADS['ed25519']}"),
    ("a claim with an empty name", f'claim-="empty" ssh-ed25519 {PAYLOADS["ed25519"]}'),
]

# (name, line) — lines the parser refuses.
BAD_LINE_CASES: list[tuple[str, str]] = [
    ("only a keytype", "ssh-ed25519"),
    ("only options", 'subject="sre:alice"'),
    ("options and a keytype but no payload", "no-pty ssh-ed25519"),
    ("a payload that is not base64", "ssh-ed25519 not-base64!!"),
]

# The file the resolver reads, mixing good lines with the shapes it must skip.
FILE_LINES = [
    "# a comment",
    "",
    "   ",
    f'subject="sre:alice",claim-role="oncall" ssh-ed25519 {PAYLOADS["ed25519"]} alice@laptop',
    # Malformed in a way that actually raises: a bare key type with no
    # payload. ("this line is malformed" parses — "line" is read as the key
    # type — so it would not exercise the skip.)
    "ssh-ed25519",
    # A revoked key, commented out. If comment lines were parsed it would
    # still resolve, which is the whole point of commenting it out.
    f"# ssh-ed25519 {PAYLOADS['ecdsa']} revoked@laptop",
    f"ssh-rsa {PAYLOADS['rsa']} bob@desktop",
    "#ssh-ed25519 commented-out",
]


def _describe_line(line: str) -> dict[str, Any]:
    """Parse one line and record what the reference made of it."""
    try:
        entry = _parse_authorized_keys_line(line)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "fingerprint": entry.fingerprint,
        "subject": entry.subject,
        "claims": dict(entry.claims),
    }


def _describe_blob(blob: bytes) -> dict[str, Any]:
    """Fingerprint one blob and record the result, or the refusal."""
    try:
        return {"ok": True, "fingerprint": fingerprint_from_openssh_blob(blob)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def _record_resolver() -> dict[str, Any]:
    """Drive the file resolver over a file with good and bad lines."""
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "authorized_keys"
        path.write_text("\n".join(FILE_LINES) + "\n", encoding="utf-8")
        resolver = AuthorizedKeysFileResolver(path)

        alice_fp = fingerprint_from_openssh_blob(f"ssh-ed25519 {PAYLOADS['ed25519']}".encode())
        bob_fp = fingerprint_from_openssh_blob(f"ssh-rsa {PAYLOADS['rsa']}".encode())
        unknown_fp = fingerprint_from_openssh_blob(f"ecdsa-sha2-nistp256 {PAYLOADS['ecdsa']}".encode())

        async def resolved(fingerprint: str, username: str = "") -> Any:
            identity = await resolver.resolve(fingerprint, pubkey_blob=b"", username=username)
            if identity is None:
                return None
            return {
                "subject": identity.subject,
                "claims": dict(identity.claims),
                "fingerprint": identity.fingerprint,
            }

        hits = {
            "alice": await resolved(alice_fp),
            "bob": await resolved(bob_fp),
            "unknown": await resolved(unknown_fp),
            "revoked": await resolved(fingerprint_from_openssh_blob(f"ssh-ed25519 {PAYLOADS['ecdsa']}".encode())),
            # The username is offered to the resolver but this one ignores it.
            "alice_other_username": await resolved(alice_fp, "someone-else"),
        }

        missing = AuthorizedKeysFileResolver(Path(raw) / "does-not-exist")
        hits["missing_file"] = await missing.resolve(alice_fp, pubkey_blob=b"", username="")

        return {
            "file_lines": FILE_LINES,
            "alice_fingerprint": alice_fp,
            "bob_fingerprint": bob_fp,
            "unknown_fingerprint": unknown_fp,
            "revoked_fingerprint": fingerprint_from_openssh_blob(f"ssh-ed25519 {PAYLOADS['ecdsa']}".encode()),
            "hits": hits,
        }


async def _main() -> int:
    """Write the golden corpus and report the case count."""
    null = NullResolver()
    corpus = {
        "payloads": PAYLOADS,
        "blobs": [{"name": name, "blob": list(blob), **_describe_blob(blob)} for name, blob in BLOB_CASES],
        "bad_blobs": [{"name": name, "blob": list(blob), **_describe_blob(blob)} for name, blob in BAD_BLOB_CASES],
        "lines": [{"name": name, "line": line, **_describe_line(line)} for name, line in LINE_CASES],
        "bad_lines": [{"name": name, "line": line, **_describe_line(line)} for name, line in BAD_LINE_CASES],
        "resolver": await _record_resolver(),
        "null_resolver": await null.resolve("SHA256:anything", pubkey_blob=b"", username="root"),
        "fingerprint_prefix": "SHA256:",
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['blobs'])} blobs, {len(corpus['lines'])} lines)")
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
