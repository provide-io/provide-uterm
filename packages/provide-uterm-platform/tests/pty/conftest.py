#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_fork_during_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """During a mutmut run ONLY, make a real ``os.fork()`` skip the test.

    ``PTYConnector``'s real-fork integration tests spawn a real child; when mutmut
    mutates ``connector.py`` a guard-defeat mutant drives one of them past its own
    patches to the REAL fork, and that child leaks into mutmut's fork-loop
    ``os.wait()`` reaper -> whole-run crash. Tests that patch ``os.fork`` themselves
    (the mocked child-path suites) override this and run normally; tests that rely
    on a real fork hit this and are skipped, so no real child is ever spawned.
    Keyed on ``MUTANT_UNDER_TEST`` (absent under normal pytest, so real-fork tests
    run unchanged outside mutation).
    """
    if not os.environ.get("MUTANT_UNDER_TEST"):
        return
    from provide.uterm.pty import connector as _conn

    def _skip_fork() -> int:
        pytest.skip("real os.fork() disabled during mutation (would leak into mutmut's reaper)")

    monkeypatch.setattr(_conn.os, "fork", _skip_fork, raising=False)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "requires_pam: requires /etc/pam.d/provide-uterm")
    config.addinivalue_line(
        "markers",
        "requires_pam_auth: requires /etc/pam.d/provide-uterm with working auth "
        "(skipped in Docker — unix_chkpwd hangs on wrong credentials)",
    )
    config.addinivalue_line("markers", "requires_root: requires root or CAP_SETUID")
