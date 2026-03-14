#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Check installed dependency licenses against an explicit allowlist."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ALLOWED_LICENSES: frozenset[str] = frozenset(
    {
        "MIT",
        "MIT License",
        "MIT OR Apache-2.0",
        "Apache-2.0",
        "Apache Software License",
        "Apache-2.0 OR BSD-2-Clause",
        "BSD",
        "BSD License",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "3-Clause BSD License",
        "ISC",
        "ISC License",
        "PSF-2.0",
        "Python Software Foundation License",
        "MPL-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "CC0 1.0 Universal (CC0 1.0) Public Domain Dedication",
        "Public Domain",
        "AGPL-3.0-or-later",
        "GNU Affero General Public License v3 or later (AGPLv3+)",
        "GNU Library or Lesser General Public License (LGPL)",
        "LGPL-3.0-or-later",
        "Dual License",
        "UNKNOWN",
        "EPL-2.0 OR GPL-2.0-or-later",
        "Apache-2.0 OR BSD-3-Clause",
        "MIT AND PSF-2.0",
        "GNU Lesser General Public License v3 (LGPLv3)",
        "Apache Software License; MIT License",
    }
)

# Dev-only tools that are never distributed — copyleft is acceptable here.
DEV_ONLY_SKIP: frozenset[str] = frozenset(
    {
        "codespell",  # GPL-2.0-only
        "python-debian",  # GPLv2+   (transitive dep of reuse)
        "reuse",  # GPLv3+ (SPDX compliance tool)
        "docutils",
        "pipdeptree",
        "text-unidecode",
        "uv",
    }
)


def _get_installed_licenses() -> list[dict[str, str]]:
    pip_licenses = Path(sys.executable).parent / "pip-licenses"
    result = subprocess.run(  # noqa: S603
        [str(pip_licenses), "--format=json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def main() -> int:
    packages = _get_installed_licenses()
    violations: list[str] = []
    skipped: list[str] = []

    for pkg in packages:
        name = pkg["Name"]
        license_str = pkg["License"]
        if name in DEV_ONLY_SKIP:
            skipped.append(name)
            continue
        if license_str not in ALLOWED_LICENSES:
            violations.append(f"  {name} {pkg['Version']}: {license_str!r}")

    if violations:
        print("Disallowed licenses found:")
        for v in violations:
            print(v)
        print("\nIf this is a dev-only tool, add it to DEV_ONLY_SKIP in scripts/check_licenses.py.")
        return 1

    checked = len(packages) - len(skipped)
    print(f"License check passed: {checked} packages checked, {len(skipped)} dev-only skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
