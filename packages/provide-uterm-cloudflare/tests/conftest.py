"""pytest configuration for provide-uterm-cloudflare tests.

The ``wrangler_server`` fixture starts a real ``pywrangler dev`` process for
E2E tests.  These tests are skipped by default and only run when explicitly
selected with ``-m e2e`` or when the ``E2E`` environment variable is set.

Usage:
    uv run pytest -m e2e                    # run only E2E tests
    E2E=1 uv run pytest                     # run all tests including E2E
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_CF_VENDOR_MISSING = not (_PACKAGE_ROOT / "python_modules").exists()

# Ensure the main provide-uterm src is on sys.path so `provide.uterm` is
# importable in E2E tests that use HostedSessionRuntime.  The `provide` namespace
# package can resolve to provide-engine only if its src isn't first on sys.path.
_UTERM_SRC = _PACKAGE_ROOT.parents[1] / "src"
_UTERM_SRC_STR = str(_UTERM_SRC)
if _UTERM_SRC_STR in sys.path:
    sys.path.remove(_UTERM_SRC_STR)
sys.path.insert(0, _UTERM_SRC_STR)
# Clear any cached provide namespace that doesn't include terminal.
_provide_mod = sys.modules.get("provide")
if _provide_mod is not None and not any("provide-uterm" in str(p) for p in getattr(_provide_mod, "__path__", [])):
    for _name in [k for k in sys.modules if k == "provide" or k.startswith("provide.")]:
        del sys.modules[_name]
_E2E_PORT = 8989
_E2E_BASE = f"http://127.0.0.1:{_E2E_PORT}"
_STARTUP_TIMEOUT_S = 90


# ---------------------------------------------------------------------------
# Auto-auth — mirrors packages/provide-uterm-server/tests/conftest.py so the
# cross-compat tests that hit a FastAPI app in header mode don't 401.
# ---------------------------------------------------------------------------


def _install_testclient_dev_principal_autoauth() -> None:
    """Attach admin header-mode credentials to every starlette TestClient."""
    from typing import Any as _Any

    from starlette.testclient import TestClient as _TestClient

    if getattr(_TestClient, "_uterm_devprincipal_patched", False):
        return

    _defaults = {"X-Uterm-Principal": "admin", "X-Uterm-Role": "admin"}
    _original_init = _TestClient.__init__

    def _patched_init(self: _TestClient, *args: _Any, **kwargs: _Any) -> None:
        _original_init(self, *args, **kwargs)
        for header, value in _defaults.items():
            if header not in self.headers:
                self.headers[header] = value

    _TestClient.__init__ = _patched_init  # type: ignore[method-assign]
    _TestClient._uterm_devprincipal_patched = True  # type: ignore[attr-defined]


_install_testclient_dev_principal_autoauth()


@pytest.fixture(autouse=True)
def _close_test_sqlite_connections(monkeypatch: pytest.MonkeyPatch):
    """Close sqlite connections created by unit-test DO storage stubs."""

    original_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def tracked_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield
    for conn in reversed(connections):
        with contextlib.suppress(sqlite3.Error):
            conn.close()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: mark test as end-to-end (requires pywrangler dev)")
    config.addinivalue_line(
        "markers",
        "real_cf: mark test as requiring a real Cloudflare deployment "
        "(real KV namespace IDs, full WS push support). "
        "Skipped unless REAL_CF=1 is set.",
    )
    config.addinivalue_line(
        "markers",
        "slow: mark test as slow (>10s); skipped unless SLOW=1 or REAL_CF=1",
    )
    config.addinivalue_line(
        "markers",
        "playwright: mark test as a Playwright browser UI test "
        "(requires: playwright install; run headed with --headed)",
    )
    if _CF_VENDOR_MISSING:
        warnings.warn(
            "CF vendor tree (python_modules/) is absent -- "
            "vendor-guard test will skip. Run 'pywrangler sync --force' "
            "from packages/provide-uterm-cloudflare/ to populate it.",
            pytest.PytestWarning,
            stacklevel=1,
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip e2e tests unless -m e2e or E2E env var is set.
    Skip real_cf tests unless REAL_CF=1 is also set.
    """
    run_real_cf = bool(os.environ.get("REAL_CF"))
    run_slow = bool(os.environ.get("SLOW")) or run_real_cf
    # REAL_CF=1 implies E2E=1 (real_cf tests are a superset of e2e tests).
    run_e2e = (
        bool(os.environ.get("E2E"))
        or run_real_cf
        or any("e2e" in str(m) for m in getattr(config.option, "markexpr", "").split())
    )
    # The worker is jwt-only (the AUTH_MODE=dev auth bypass was removed). Local
    # pywrangler-dev e2e tests that hit authenticated routes therefore need a
    # real JWT; without one every such request 401s. REAL_CF runs authenticate
    # at the edge with CF Access service tokens instead, so this gate only
    # applies to the local (non-REAL_CF) path. Set CF_E2E_JWT to opt in once the
    # request helpers are wired to send it.
    local_e2e_unauthed = run_e2e and not run_real_cf and not os.environ.get("CF_E2E_JWT")
    for item in items:
        if item.get_closest_marker("slow") and not run_slow:
            item.add_marker(pytest.mark.skip(reason="slow tests skipped; set SLOW=1 or REAL_CF=1"))
        if item.get_closest_marker("real_cf") and not run_real_cf:
            item.add_marker(pytest.mark.skip(reason="requires real CF deployment; set REAL_CF=1"))
        elif item.get_closest_marker("e2e") and not run_e2e:
            item.add_marker(pytest.mark.skip(reason="E2E tests skipped; use -m e2e or set E2E=1"))
        elif item.get_closest_marker("e2e") and local_e2e_unauthed:
            item.add_marker(
                pytest.mark.skip(
                    reason="CF e2e tests need a JWT (the AUTH_MODE 'dev' bypass was removed); set CF_E2E_JWT"
                )
            )


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Print a visible banner when CF vendor tree is missing."""
    if not _CF_VENDOR_MISSING:
        return
    tw = terminalreporter._tw
    tw.sep("!", "CF VENDOR TREE MISSING")
    tw.line("packages/provide-uterm-cloudflare/python_modules/ is absent.")
    tw.line("The vendor-guard test (test_ushell_vendor_guard) was SKIPPED.")
    tw.line("To populate: cd packages/provide-uterm-cloudflare && pywrangler sync --force")
    tw.sep("!")


def _wait_for_health(base: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=2) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    return False


class _PywranglerManager:
    """Manages a pywrangler dev process with auto-restart on crash."""

    def __init__(self) -> None:
        import shutil

        self._pywrangler = shutil.which("pywrangler") or "pywrangler"
        self._proc: subprocess.Popen[bytes] | None = None
        self._dev_vars_path = _PACKAGE_ROOT / ".dev.vars"
        self._dev_vars_original: str | None = (
            self._dev_vars_path.read_text(encoding="utf-8") if self._dev_vars_path.exists() else None
        )
        # The worker is jwt-only: config.from_env rejects the removed dev/none
        # modes, so writing AUTH_MODE=dev made the worker 500 on every request
        # (it booted, but /api/health never returned 200, so the fixture skipped
        # the whole suite with a misleading "did not start"). Write a bootable
        # jwt config instead — a real AUTH_MODE plus a worker bearer token that
        # clears config's entropy floor; JWT_* come from wrangler.toml [vars].
        # Routes that require a principal still 401 without a JWT, so e2e tests
        # are gated on CF_E2E_JWT in pytest_collection_modifyitems.
        import secrets

        self._dev_vars_path.write_text(
            f"AUTH_MODE=jwt\nWORKER_BEARER_TOKEN={secrets.token_urlsafe(48)}\n",
            encoding="utf-8",
        )

    def start(self) -> bool:
        self._stop_proc()
        self._proc = subprocess.Popen(
            [
                self._pywrangler,
                "dev",
                "--port",
                str(_E2E_PORT),
                "--ip",
                "127.0.0.1",
                "--var",
                "ENVIRONMENT:development",
            ],
            cwd=_PACKAGE_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return _wait_for_health(_E2E_BASE, _STARTUP_TIMEOUT_S)

    def ensure_healthy(self) -> bool:
        """Check if pywrangler is alive; restart if crashed."""
        if self._proc is not None and self._proc.poll() is None:
            # Process still running — quick health check
            try:
                with urllib.request.urlopen(f"{_E2E_BASE}/api/health", timeout=3) as resp:  # noqa: S310
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
        # Dead or unhealthy — restart
        return self.start()

    def _stop_proc(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    def teardown(self) -> None:
        self._stop_proc()
        if self._dev_vars_original is None:
            self._dev_vars_path.unlink(missing_ok=True)
        else:
            self._dev_vars_path.write_text(self._dev_vars_original, encoding="utf-8")


# Module-level manager so we can share it between fixtures.
_manager: _PywranglerManager | None = None


@pytest.fixture(scope="session")
def wrangler_server():
    """Yield the base URL of a running worker for E2E tests.

    If ``REAL_CF_URL`` is set, that URL is yielded directly.
    Otherwise, starts ``pywrangler dev`` locally with auto-restart on crash.
    """
    real_cf_url = os.environ.get("REAL_CF_URL", "").rstrip("/")
    # Only use real CF URL when REAL_CF is explicitly set — avoids accidentally
    # routing local e2e tests to the real deployment when REAL_CF_URL lingers in env.
    if real_cf_url and os.environ.get("REAL_CF"):
        yield real_cf_url
        return

    global _manager
    _manager = _PywranglerManager()
    if not _manager.start():
        _manager.teardown()
        pytest.skip(f"pywrangler dev did not start within {_STARTUP_TIMEOUT_S}s")

    yield _E2E_BASE

    _manager.teardown()
    _manager = None


@pytest.fixture(autouse=True)
def _ensure_pywrangler_healthy(request: pytest.FixtureRequest) -> None:
    """Auto-use fixture: before each E2E test, verify pywrangler is alive."""
    if not request.node.get_closest_marker("e2e"):
        return
    if _manager is not None and not _manager.ensure_healthy():
        pytest.skip("pywrangler crashed and could not be restarted")
