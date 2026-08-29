#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The install spec release verification uses, and why it is not just the name.

Verification installs each published package and imports every name in its
``import_names``. A dependency that lives behind an extra is absent from a bare
install, so the import fails even though the artifact is fine -- which is how the
0.5.1 release died at ``Verify TestPyPI · provide-uterm-platform`` with
``ModuleNotFoundError: No module named 'fastapi'``, after the publish itself had
already succeeded.

Extras are therefore declared per package, and kept to the minimum: installing
extras a package does not need would have verification exercise a fatter
environment than the one users get, which is the opposite of the point.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.package_metadata import PUBLISHED_PACKAGES


def test_the_manager_extra_is_declared_for_platform() -> None:
    """provide.uterm.manager imports fastapi eagerly, and fastapi is an extra."""
    platform = next(p for p in PUBLISHED_PACKAGES if p.name == "provide-uterm-platform")
    assert "manager" in platform.install_extras
    assert platform.install_spec == "provide-uterm-platform[manager]"


def test_packages_that_import_cleanly_declare_no_extras() -> None:
    """Only the package that needs an extra carries one.

    provide-uterm-server and provide-uterm-client both import their declared
    names from a bare install -- their heavier dependencies sit behind lazy
    imports -- so requesting extras for them would install more than a user gets
    and hide a real packaging regression rather than catch it.
    """
    bare = {p.name for p in PUBLISHED_PACKAGES if not p.install_extras}
    assert bare == {
        "provide-uterm",
        "provide-uterm-server",
        "provide-uterm-client",
        "provide-uterm-cloudflare",
    }


def test_install_spec_is_bare_name_without_extras() -> None:
    core = next(p for p in PUBLISHED_PACKAGES if p.name == "provide-uterm")
    assert core.install_spec == "provide-uterm"


def test_the_cli_prints_the_spec_shell_callers_need() -> None:
    """ci/install_from_testpypi.sh reads the table through this entry point."""
    for package in PUBLISHED_PACKAGES:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "package_metadata.py"), package.name],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == package.install_spec


def test_the_cli_rejects_an_unknown_package() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "package_metadata.py"), "not-a-package"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "unknown package" in result.stderr
