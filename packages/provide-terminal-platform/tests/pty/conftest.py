#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import pytest


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
