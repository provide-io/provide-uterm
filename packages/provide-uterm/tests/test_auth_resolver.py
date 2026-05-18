#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for ``AuthorizedKeysFileResolver.resolve`` and ``_load_entries``.

File-level resolver behaviour: missing files, blank/comment lines, malformed
entries, UTF-8 subject decoding, path argument acceptance, and the surgical
mutation-killers around ``strip()``/``startswith("#")`` short-circuiting.
"""

from __future__ import annotations

import tempfile

import pytest

from provide.uterm.auth import (
    AuthorizedKeysFileResolver,
    fingerprint_from_openssh_blob,
)

_ED25519_KEYTYPE = "ssh-ed25519"
_ED25519_PAYLOAD_B64 = "AAAAC3NzaC1lZDI1NTE5AAAAIK7nKaxTKmzX0z3V6tGqmmOvkSiGXh3yF2J5vqkQTOY+"


@pytest.fixture()
def keys_file(tmp_path):  # type: ignore[no-untyped-def]
    return tmp_path / "authorized_keys"


async def test_resolver_resolve_returns_none_for_fingerprint_miss(keys_file) -> None:  # type: ignore[no-untyped-def]
    keys_file.write_text(f"{_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} alice@laptop\n")
    resolver = AuthorizedKeysFileResolver(keys_file)
    out = await resolver.resolve(
        fingerprint="SHA256:does-not-match",
        pubkey_blob=b"",
        username="anyone",
    )
    assert out is None


async def test_resolver_resolve_returns_identity_with_matching_fingerprint(keys_file) -> None:  # type: ignore[no-untyped-def]
    line = f"{_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} alice@laptop"
    keys_file.write_text(line + "\n")
    expected_fp = fingerprint_from_openssh_blob(line.encode("ascii"))
    resolver = AuthorizedKeysFileResolver(keys_file)
    identity = await resolver.resolve(
        fingerprint=expected_fp,
        pubkey_blob=b"",
        username="alice",
    )
    assert identity is not None
    assert identity.fingerprint == expected_fp
    assert identity.subject == "alice@laptop"


async def test_resolver_resolve_ignores_username_when_fingerprint_matches(keys_file) -> None:  # type: ignore[no-untyped-def]
    # Resolver dispatches on fingerprint only; the username arg must NOT
    # gate the match (changing the resolver to require username == subject
    # would silently break SSH integrations).
    line = f"{_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} alice@laptop"
    keys_file.write_text(line + "\n")
    fp = fingerprint_from_openssh_blob(line.encode("ascii"))
    resolver = AuthorizedKeysFileResolver(keys_file)
    out = await resolver.resolve(fingerprint=fp, pubkey_blob=b"", username="bob")
    assert out is not None
    assert out.subject == "alice@laptop"


def test_resolver_load_entries_returns_empty_when_file_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    resolver = AuthorizedKeysFileResolver(tmp_path / "absent")
    # _load_entries is the synchronous arm of resolve; missing file → [].
    assert resolver._load_entries() == []


def test_resolver_load_entries_skips_blank_and_comment_lines(keys_file) -> None:  # type: ignore[no-untyped-def]
    body = "\n".join(
        [
            "",
            "# leading comment",
            "   ",
            "  # indented comment",
            f"{_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} alice@laptop",
            "",
            "# trailing comment",
        ]
    )
    keys_file.write_text(body)
    resolver = AuthorizedKeysFileResolver(keys_file)
    entries = resolver._load_entries()
    assert len(entries) == 1
    assert entries[0].subject == "alice@laptop"


def test_resolver_load_entries_continues_past_malformed_line(keys_file) -> None:  # type: ignore[no-untyped-def]
    body = "\n".join(
        [
            "ssh-ed25519",  # malformed: missing payload
            f"{_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} alice@laptop",
        ]
    )
    keys_file.write_text(body)
    resolver = AuthorizedKeysFileResolver(keys_file)
    entries = resolver._load_entries()
    # Exactly one entry survives — the malformed line MUST be skipped,
    # not crash the entire file load.
    assert len(entries) == 1
    assert entries[0].subject == "alice@laptop"


def test_resolver_load_entries_reads_utf8_subject_correctly(keys_file) -> None:  # type: ignore[no-untyped-def]
    # Encoding mutation: switching read_text(encoding="utf-8") to a
    # different codec would mangle non-ASCII subjects. Pin UTF-8.
    line = f'subject="óscar" {_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} fallback'
    keys_file.write_text(line, encoding="utf-8")
    resolver = AuthorizedKeysFileResolver(keys_file)
    entries = resolver._load_entries()
    assert len(entries) == 1
    assert entries[0].subject == "óscar"


def test_resolver_path_argument_accepts_string_and_pathlib(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "k"
    p.write_text("")
    r1 = AuthorizedKeysFileResolver(str(p))
    r2 = AuthorizedKeysFileResolver(p)
    assert r1._path == r2._path  # type: ignore[attr-defined]


def test_resolver_load_entries_strips_each_line_independently() -> None:
    # ``line = raw.strip()`` must remove leading + trailing whitespace
    # from each line individually before deciding blank/comment.
    body = "\n".join(
        [
            f"   {_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} alice  ",
            "  # indented comment with trailing whitespace  ",
        ]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".keys", delete=False) as f:
        f.write(body)
        path = f.name
    resolver = AuthorizedKeysFileResolver(path)
    entries = resolver._load_entries()
    assert len(entries) == 1
    assert entries[0].subject == "alice"


def test_resolver_load_entries_or_short_circuits_on_empty_line_before_comment_check() -> None:
    # ``if not line or line.startswith("#")``: an empty line shouldn't
    # ever reach the startswith() probe. Pin by stress-testing both
    # halves of the boolean independently.
    with tempfile.NamedTemporaryFile("w", suffix=".keys", delete=False) as f:
        # Empty line (handled by `not line`), then comment line (handled
        # by `line.startswith("#")`), then a real entry.
        f.write(f"\n# this is a comment\n{_ED25519_KEYTYPE} {_ED25519_PAYLOAD_B64} z\n")
        path = f.name
    entries = AuthorizedKeysFileResolver(path)._load_entries()
    assert len(entries) == 1
    assert entries[0].subject == "z"
