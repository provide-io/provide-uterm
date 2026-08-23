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


def test_a_library_older_than_its_sources_names_them(built, monkeypatch) -> None:
    """The warning has to say which sources moved, and what fixes it.

    Asserts on the structured event rather than the rendered log line. The
    values here are filesystem paths, and provide-telemetry redacts values
    carrying a high-entropy segment: on macOS ``$TMPDIR`` looks like
    ``/var/folders/sg/wy47gw996f78fznt898m8x540000gn/T/``, so ``library`` and
    ``remedy`` both render as ``***`` and an assertion on ``caplog.text`` fails.
    Linux CI's ``/tmp/pytest-of-runner/pytest-0/`` has no such segment, so this
    passed there and failed for every macOS developer. What the test is
    actually about is the content of the warning, which the kwargs carry
    verbatim.
    """

    lib, source_dir = built
    monkeypatch.setattr(_build, "_source_dir", lambda: source_dir)
    os.utime(source_dir / "capture.c", None)  # edited after the build

    events: list[tuple[str, dict[str, object]]] = []

    class _Recorder:
        """Stands in for the module logger; its instance uses __slots__."""

        @staticmethod
        def warning(event: str, **kwargs: object) -> None:
            events.append((event, kwargs))

    monkeypatch.setattr(_build, "logger", _Recorder)

    _build._warn_if_stale(lib)

    assert len(events) == 1
    event, fields = events[0]
    assert event == "capture_library_is_stale"
    assert fields["newer_sources"] == ["capture.c"]
    # remedy and remedy_dir are SEPARATE fields, not one interpolated string.
    # Telemetry's secret scanner redacts a whole field value when any part of it
    # looks like a credential, and macOS $TMPDIR carries a high-entropy segment —
    # so a remedy with the directory inside it came out redacted in exactly the
    # case a developer needed to read it. Asserting them apart is what keeps
    # them apart.
    assert "make" in str(fields["remedy"])
    assert str(source_dir) not in str(fields["remedy"])
    assert str(source_dir) == str(fields["remedy_dir"])
    assert str(lib) == fields["library"]


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
