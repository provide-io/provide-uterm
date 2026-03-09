"""pytest configuration for provide-terminal-cloudflare tests.

The ``wrangler_server`` fixture starts a real ``pywrangler dev`` process for
E2E tests.  These tests are skipped by default and only run when explicitly
selected with ``-m e2e`` or when the ``E2E`` environment variable is set.

Usage:
    uv run pytest -m e2e                    # run only E2E tests
    E2E=1 uv run pytest                     # run all tests including E2E
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_E2E_PORT = 8989
_E2E_BASE = f"http://127.0.0.1:{_E2E_PORT}"
_STARTUP_TIMEOUT_S = 90


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: mark test as end-to-end (requires pywrangler dev)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip e2e tests unless -m e2e or E2E env var is set."""
    run_e2e = os.environ.get("E2E") or any("e2e" in str(m) for m in getattr(config.option, "markexpr", "").split())
    if not run_e2e:
        skip = pytest.mark.skip(reason="E2E tests skipped; use -m e2e or set E2E=1")
        for item in items:
            if item.get_closest_marker("e2e"):
                item.add_marker(skip)


@pytest.fixture(scope="session")
def wrangler_server():
    """Start ``pywrangler dev`` and wait until the health endpoint responds.

    Yields the base URL of the local dev server.  The process is terminated
    when the test session ends.
    """
    import shutil

    pywrangler = shutil.which("pywrangler") or "pywrangler"
    proc = subprocess.Popen(  # noqa: S603
        [pywrangler, "dev", "--port", str(_E2E_PORT), "--ip", "127.0.0.1"],
        cwd=_PACKAGE_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{_E2E_BASE}/api/health", timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    ready = True
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(1)

    if not ready:
        proc.terminate()
        out, _ = proc.communicate(timeout=5)
        msg = (out or b"").decode(errors="replace")
        pytest.skip(f"pywrangler dev did not start within {_STARTUP_TIMEOUT_S}s: {msg[:500]}")

    yield _E2E_BASE

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
