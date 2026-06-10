#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cover the lazy ``provide.uterm.frames`` attribute on the core package.

``provide/uterm/__init__.py`` exposes the ``frames`` builder facade via a module
``__getattr__`` (deferred to avoid importing it eagerly). Only a Cloudflare-suite
test exercises it, so the strict core-only coverage gate missed the branch; these
two tests cover both arms directly.
"""

from __future__ import annotations

import importlib

import pytest

import provide.uterm as ut


def test_lazy_frames_attribute_returns_module() -> None:
    assert ut.__getattr__("frames") is importlib.import_module("provide.uterm.frames")


def test_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        ut.__getattr__("definitely_not_an_attribute")
