# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
from pathlib import Path

import pytest

# Docker containers have /.dockerenv; pam_unix.so's unix_chkpwd setuid helper
# hangs indefinitely on wrong credentials inside Docker, so we skip tests
# that require PAM authentication failure in that environment.
_IN_DOCKER = Path("/.dockerenv").exists()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "requires_pam: requires /etc/pam.d/provide-terminal"
    )
    config.addinivalue_line(
        "markers",
        "requires_pam_auth: requires /etc/pam.d/provide-terminal with working auth "
        "(skipped in Docker — unix_chkpwd hangs on wrong credentials)",
    )
    config.addinivalue_line("markers", "requires_root: requires root or CAP_SETUID")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    pam_svc = Path("/etc/pam.d/provide-terminal")
    for item in items:
        if item.get_closest_marker("requires_root") and os.getuid() != 0:
            item.add_marker(pytest.mark.skip(reason="requires root or CAP_SETUID"))
        if item.get_closest_marker("requires_pam") and not pam_svc.exists():
            item.add_marker(
                pytest.mark.skip(reason="requires /etc/pam.d/provide-terminal")
            )
        if item.get_closest_marker("requires_pam_auth") and (
            not pam_svc.exists() or _IN_DOCKER
        ):
            item.add_marker(
                pytest.mark.skip(
                    reason=(
                        "requires /etc/pam.d/provide-terminal with working unix_chkpwd "
                        "(skipped in Docker — unix_chkpwd hangs on wrong credentials)"
                    )
                )
            )
