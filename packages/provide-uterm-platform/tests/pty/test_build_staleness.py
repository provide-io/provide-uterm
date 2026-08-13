#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The check that a checkout is not running a library older than its sources.

A stale capture library loads, connects, and behaves like the code being read,
so nothing else in the system can tell the difference. This is the only place
that says so.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from provide.uterm.pty import _build


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, Path]:
    """A library and its sources, laid out as the checkout lays them out."""

    source_dir = tmp_path / "native" / "capture"
    source_dir.mkdir(parents=True)
    native_dir = tmp_path / "src" / "provide" / "uterm" / "pty" / "_native"
    native_dir.mkdir(parents=True)
    lib = native_dir / "libuterm_capture.so"
    lib.write_bytes(b"\x7fELF")
    for name in _build._CAPTURE_SOURCES:
        source = source_dir / name
        source.write_text("/* source */")
        os.utime(source, (0, 0))  # older than the library
    return lib, source_dir


def test_a_library_newer_than_its_sources_says_nothing(built, monkeypatch, caplog) -> None:
    lib, source_dir = built
    monkeypatch.setattr(_build, "_source_dir", lambda: source_dir)

    _build._warn_if_stale(lib)

    assert "capture_library_is_stale" not in caplog.text


def test_a_library_older_than_its_sources_names_them(built, monkeypatch, caplog) -> None:
    """The warning has to say which sources moved, and what fixes it."""

    lib, source_dir = built
    monkeypatch.setattr(_build, "_source_dir", lambda: source_dir)
    os.utime(source_dir / "capture.c", None)  # edited after the build

    with caplog.at_level("WARNING"):
        _build._warn_if_stale(lib)

    assert "capture_library_is_stale" in caplog.text
    assert "capture.c" in caplog.text
    assert "make" in caplog.text


def test_an_installed_package_has_no_sources_to_be_behind(built, monkeypatch, caplog) -> None:
    """A wheel carries the library and none of the sources; that is not stale."""

    lib, _ = built
    monkeypatch.setattr(_build, "_source_dir", lambda: Path("/nonexistent/native/capture"))

    _build._warn_if_stale(lib)

    assert "capture_library_is_stale" not in caplog.text


def test_an_unreadable_library_is_not_reported_as_stale(built, monkeypatch, caplog) -> None:
    lib, source_dir = built
    monkeypatch.setattr(_build, "_source_dir", lambda: source_dir)
    lib.unlink()

    _build._warn_if_stale(lib)

    assert "capture_library_is_stale" not in caplog.text


def test_a_missing_library_is_reported_as_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_build, "__file__", str(tmp_path / "_build.py"))

    assert _build.get_capture_lib_path() is None
