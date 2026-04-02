# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "requires_pam: requires /etc/pam.d/provide-terminal"
    )
    config.addinivalue_line("markers", "requires_root: requires root or CAP_SETUID")
