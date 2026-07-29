#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the SPDX header normalizer.

The normalizer rewrites the top of every Python file in the repository, so
what it does to the lines it was not asked about matters more than what it
does to the header. Two comments that live directly under the SPDX block are
read by tooling:

* ``# uv-package: <name>`` tells ``.ci/check_ts_goldens.sh`` which workspace
  package to run a golden-corpus generator under;
* ``# Mutation-enforced at killed==100`` records a file's mutation perimeter
  and its bound kill-suite.

Losing either is silent: the file still has a valid header, and the gate that
depended on the comment fails somewhere else entirely, or stops running.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location("spdx_headers", _SCRIPTS / "spdx_headers.py")
assert _spec is not None and _spec.loader is not None
spdx_headers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spdx_headers)

_CANONICAL = "".join(spdx_headers.CANONICAL_BLOCK)
# Built rather than written: a literal tag here would be read as this
# file's own licence, and `reuse lint` refuses the trailing escape.
_COPYRIGHT = spdx_headers.SPDX_COPYRIGHT
_LICENSE = spdx_headers.SPDX_LICENSE


class TestItAddsAHeader:
    def test_a_file_with_no_header_gets_one(self) -> None:
        assert spdx_headers.normalize_python_text('"""Doc."""\n') == _CANONICAL + '"""Doc."""\n'

    def test_a_file_that_already_has_one_is_left_alone(self) -> None:
        text = _CANONICAL + '"""Doc."""\n'
        assert spdx_headers.normalize_python_text(text) == text

    def test_a_header_missing_its_delimiters_is_completed(self) -> None:
        # The two files this was written for: an SPDX pair with no opening or
        # closing `#` line.
        loose = _COPYRIGHT + _LICENSE + '"""Doc."""\n'
        assert spdx_headers.normalize_python_text(loose) == _CANONICAL + '"""Doc."""\n'

    def test_a_shebang_stays_first(self) -> None:
        text = "#!/usr/bin/env python3\n" + _CANONICAL + '"""Doc."""\n'
        assert spdx_headers.normalize_python_text(text) == text


class TestItKeepsTheCommentsItWasNotAskedAbout:
    def test_the_uv_package_marker_survives(self) -> None:
        # A generator whose marker is stripped is run under the wrong
        # workspace package, so its import fails and the drift check breaks.
        text = _CANONICAL + "# uv-package: provide-uterm-cloudflare\n" + '"""Doc."""\n'
        assert spdx_headers.normalize_python_text(text) == text

    def test_the_mutation_perimeter_note_survives(self) -> None:
        note = "# Mutation-enforced at killed==100 ([tool.mutmut]); bound suite: tests/x.py\n"
        text = _CANONICAL + note + '"""Doc."""\n'
        assert spdx_headers.normalize_python_text(text) == text

    def test_several_comments_keep_their_order(self) -> None:
        text = _CANONICAL + "# first\n# second\n" + '"""Doc."""\n'
        assert spdx_headers.normalize_python_text(text) == text

    def test_a_comment_survives_a_header_that_had_to_be_repaired(self) -> None:
        loose = _COPYRIGHT + _LICENSE + "# uv-package: provide-uterm\n" + '"""Doc."""\n'
        assert (
            spdx_headers.normalize_python_text(loose) == _CANONICAL + "# uv-package: provide-uterm\n" + '"""Doc."""\n'
        )

    def test_a_comment_above_the_header_is_kept_too(self) -> None:
        # Moved below the block rather than dropped: the header has to come
        # first, but nothing here is entitled to delete somebody's line.
        text = "# a note\n" + _CANONICAL + '"""Doc."""\n'
        assert spdx_headers.normalize_python_text(text) == _CANONICAL + "# a note\n" + '"""Doc."""\n'

    def test_a_duplicated_header_collapses_to_one(self) -> None:
        assert spdx_headers.normalize_python_text(_CANONICAL + _CANONICAL + "x = 1\n") == _CANONICAL + "x = 1\n"

    def test_the_space_an_author_left_under_the_header_is_left_alone(self) -> None:
        # Blank lines below the block belong to whoever wrote the file. The
        # normalizer's job is the header, and tidying anything else is how it
        # came to be deleting comments in the first place.
        assert spdx_headers.normalize_python_text(_CANONICAL + "\n\nx = 1\n") == _CANONICAL + "\n\nx = 1\n"


class TestTheRepositoryItself:
    def test_normalizing_every_file_would_change_nothing_but_the_header(self) -> None:
        # The regression that prompted this: running the normalizer over the
        # tree deleted 38 comment lines across the packages. Normalizing a
        # file that already has a canonical header must be a no-op, whatever
        # else is in it.
        for path in sorted((_ROOT / "packages").rglob("*.py")):
            if any(part in spdx_headers.EXCLUDED_DIRS for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8")
            if not spdx_headers.has_canonical_header(text):
                continue
            assert spdx_headers.normalize_python_text(text) == text, f"{path} would be rewritten"
