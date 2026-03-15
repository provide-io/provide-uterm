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
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# Ensure the main provide-terminal src is on sys.path so `provide.terminal` is
# importable in E2E tests that use HostedSessionRuntime.  The `provide` namespace
# package can resolve to provide-engine only if its src isn't first on sys.path.
_UTERM_SRC = _PACKAGE_ROOT.parents[1] / "src"
_UTERM_SRC_STR = str(_UTERM_SRC)
if _UTERM_SRC_STR in sys.path:
    sys.path.remove(_UTERM_SRC_STR)
sys.path.insert(0, _UTERM_SRC_STR)
# Clear any cached provide namespace that doesn't include terminal.
_provide_mod = sys.modules.get("provide")
if _provide_mod is not None and not any("provide-terminal" in str(p) for p in getattr(_provide_mod, "__path__", [])):
    for _name in [k for k in sys.modules if k == "provide" or k.startswith("provide.")]:
        del sys.modules[_name]
_E2E_PORT = 8989
_E2E_BASE = f"http://127.0.0.1:{_E2E_PORT}"
_STARTUP_TIMEOUT_S = 90


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
    for item in items:
        if item.get_closest_marker("slow") and not run_slow:
            item.add_marker(pytest.mark.skip(reason="slow tests skipped; set SLOW=1 or REAL_CF=1"))
        if item.get_closest_marker("real_cf") and not run_real_cf:
            item.add_marker(pytest.mark.skip(reason="requires real CF deployment; set REAL_CF=1"))
        elif item.get_closest_marker("e2e") and not run_e2e:
            item.add_marker(pytest.mark.skip(reason="E2E tests skipped; use -m e2e or set E2E=1"))


@pytest.fixture(scope="session")
def wrangler_server():
    """Yield the base URL of a running worker for E2E tests.

    If ``REAL_CF_URL`` is set (e.g. ``https://provide-terminal-cloudflare.neurotic.workers.dev``),
    that URL is yielded directly — no local pywrangler dev process is started.

    Otherwise, starts ``pywrangler dev`` locally, writes ``.dev.vars`` with
    ``AUTH_MODE=dev`` (restored on teardown), waits for the health endpoint.
    """
    real_cf_url = os.environ.get("REAL_CF_URL", "").rstrip("/")
    if real_cf_url:
        yield real_cf_url
        return

    import shutil

    dev_vars_path = _PACKAGE_ROOT / ".dev.vars"
    _dev_vars_original: str | None = dev_vars_path.read_text(encoding="utf-8") if dev_vars_path.exists() else None
    dev_vars_path.write_text("AUTH_MODE=dev\nWORKER_BEARER_TOKEN=e2e-worker-token\n", encoding="utf-8")

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
        if _dev_vars_original is None:
            dev_vars_path.unlink(missing_ok=True)
        else:
            dev_vars_path.write_text(_dev_vars_original, encoding="utf-8")
        pytest.skip(f"pywrangler dev did not start within {_STARTUP_TIMEOUT_S}s: {msg[:500]}")

    yield _E2E_BASE

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    if _dev_vars_original is None:
        dev_vars_path.unlink(missing_ok=True)
    else:
        dev_vars_path.write_text(_dev_vars_original, encoding="utf-8")
