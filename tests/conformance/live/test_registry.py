#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for which drivers exist and which are missing.

The failure this guards against is an incomplete matrix reading like a
complete one — four green cells and sixteen green cells print the same
summary — so every gap has to come back named.
"""

from __future__ import annotations

from pathlib import Path

from harness.registry import CLIENT, LANGUAGES, REGISTRY, REPO_ROOT, SERVER, Registration, available


class TestTheRealRegistry:
    def test_it_knows_about_every_language_in_the_repository(self) -> None:
        assert {registration.language for registration in REGISTRY} == set(LANGUAGES)

    def test_python_is_available_in_both_roles(self) -> None:
        # The reference is the one driver that must always be there: without
        # it there is nothing for the other cells to be compared against.
        found = available(REPO_ROOT, only=["python"])
        assert [spec.language for spec in found.servers] == ["python"]
        assert [spec.language for spec in found.clients] == ["python"]
        assert found.gaps == ()

    def test_a_language_that_is_only_a_client_says_so_rather_than_vanishing(self) -> None:
        registration = next(one for one in REGISTRY if one.language == "typescript")
        assert registration.roles == frozenset({CLIENT})
        found = available(REPO_ROOT, only=["typescript"])
        assert found.servers == ()
        assert any("no server role" in gap for gap in found.gaps)

    def test_the_cell_count_is_reported_so_a_short_matrix_is_visible(self) -> None:
        found = available(REPO_ROOT, only=["python", "typescript"])
        assert found.cell_count == len(found.servers) * len(found.clients)


class TestMissingDrivers:
    def test_a_driver_whose_files_are_not_there_is_a_gap_not_a_crash(self, tmp_path: Path) -> None:
        found = available(tmp_path, only=["python"])
        assert found.servers == ()
        assert found.clients == ()
        assert "not built" in found.gaps[0]

    def test_the_gap_names_the_file_that_is_missing(self, tmp_path: Path) -> None:
        assert "driver.py" in available(tmp_path, only=["python"]).gaps[0]

    def test_a_driver_whose_toolchain_is_absent_is_a_gap(self, tmp_path: Path) -> None:
        (tmp_path / "there").write_text("")
        registration = Registration(
            language="go",
            roles=frozenset({CLIENT, SERVER}),
            build=lambda root: (_ for _ in ()).throw(AssertionError("must not be built")),
            needs_files=("there",),
            needs_tools=("a-compiler-nobody-has",),
        )
        found = available(tmp_path, registry=[registration])
        assert found.gaps == ("go: a-compiler-nobody-has not on PATH",)

    def test_asking_for_one_language_does_not_run_the_others(self, tmp_path: Path) -> None:
        # `--servers go` must not report the absence of every other language.
        assert available(tmp_path, only=["python"]).gaps == available(tmp_path, only=["python"]).gaps
        assert len(available(tmp_path, only=["python"]).gaps) == 1
