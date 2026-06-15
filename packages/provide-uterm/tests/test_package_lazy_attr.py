#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Cover lazy module attributes on the core package.

``provide/uterm/__init__.py`` exposes selected facades through module
``__getattr__`` (deferred to avoid importing them eagerly). These tests cover
both known lazy modules and the unknown-attribute arm directly.
"""

from __future__ import annotations

import importlib

import pytest

import provide.uterm as ut


def test_lazy_frames_attribute_returns_module() -> None:
    assert ut.__getattr__("frames") is importlib.import_module("provide.uterm.frames")


def test_lazy_channels_attribute_returns_module() -> None:
    assert ut.__getattr__("channels") is importlib.import_module("provide.uterm.channels")


def test_unknown_attribute_raises() -> None:
    with pytest.raises(AttributeError):
        ut.__getattr__("definitely_not_an_attribute")
