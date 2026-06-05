#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Full-perimeter-gate survivor for ``auth`` — the authorized_keys file read.

A full (non ``--changed-only``) mutmut run surfaced that
``AuthorizedKeysFileResolver._load_entries`` reads the key file with an explicit
``encoding="utf-8"`` that no existing suite pinned. ``read_text`` is wrapped to
record the encoding so the exact literal is asserted (``None`` ⇒ locale default;
``"UTF-8"`` ⇒ wrong literal). The other auth survivors from that run are
documented equivalents in ``mutation_equivalents.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from provide.uterm.auth import AuthorizedKeysFileResolver


def test_load_entries_reads_file_as_utf8(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The authorized_keys file is read with an explicit ``encoding="utf-8"``."""
    keyfile = tmp_path / "authorized_keys"
    keyfile.write_text("ssh-ed25519 AAAA comment\n", encoding="utf-8")
    resolver = AuthorizedKeysFileResolver(keyfile)

    real_read_text = Path.read_text
    encodings: list[str | None] = []

    def _wrapped(self: Path, *args: object, **kwargs: object) -> str:
        encodings.append(kwargs.get("encoding"))  # type: ignore[arg-type]
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _wrapped)

    resolver._load_entries()  # malformed line is tolerated; the read still happens

    assert encodings, "expected the key file to be read"
    assert all(enc == "utf-8" for enc in encodings)  # exact literal (not None / 'UTF-8')
