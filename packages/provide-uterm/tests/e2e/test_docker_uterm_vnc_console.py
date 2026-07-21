#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Docker-marked e2e: first-party uterm VNC console via prove_uterm_vnc_console.

Exercises the shipped proof script (lab + server + binary /gui/vnc) — not a
reimplementation of the dial or RFB path.

Run with::

    uv run pytest packages/provide-uterm/tests/e2e/test_docker_uterm_vnc_console.py \\
        -m docker -v --no-cov --timeout=600
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prove_uterm_vnc_console as prove

_docker_missing = shutil.which("docker") is None


def _evidence_dir(tmp_path: Path) -> Path:
    for key in ("EVIDENCE_DIR", "SCRATCH"):
        val = os.environ.get(key)
        if val:
            p = Path(val) / "pytest-uterm-vnc-console"
            p.mkdir(parents=True, exist_ok=True)
            return p
    return tmp_path


def test_vnc_console_assets_and_script_exist() -> None:
    """Structural gate (no Docker): proof script + frontend source + example TOML."""
    assert (_SCRIPTS / "prove_uterm_vnc_console.py").is_file()
    assert (_SCRIPTS / "uterm-server.vnc-lab.example.toml").is_file()
    fe = _REPO_ROOT / "packages" / "provide-uterm-frontend"
    assert (fe / "vnc.html").is_file()
    assert (fe / "src" / "vnc-page.ts").is_file()
    assert (fe / "src" / "vnc-url.ts").is_file()
    pkg = (fe / "package.json").read_text(encoding="utf-8")
    assert "@novnc/novnc" in pkg
    # Dial module present in server package.
    dial = _REPO_ROOT / "packages" / "provide-uterm-server" / "src" / "provide" / "uterm" / "server" / "vnc_upstream.py"
    assert dial.is_file()
    assert "open_rfb_upstream" in dial.read_text(encoding="utf-8")


@pytest.mark.docker
@pytest.mark.slow
def test_prove_uterm_vnc_console_live(tmp_path: Path) -> None:
    """Live lab + server + two plain runs + TLS + denied (real entry point)."""
    if _docker_missing:
        pytest.skip("Docker not available")
    evidence = _evidence_dir(tmp_path)
    rc = prove.main(["--evidence-dir", str(evidence), "--runs", "2", "--skip-screenshot"])
    assert rc == 0, f"prove failed; see {evidence}"
    connect = (evidence / "vnc-console-connect.log").read_text(encoding="utf-8")
    connect2 = (evidence / "vnc-console-connect-2.log").read_text(encoding="utf-8")
    tls = (evidence / "vnc-console-tls.log").read_text(encoding="utf-8")
    denied = (evidence / "vnc-console-denied.log").read_text(encoding="utf-8")
    assert "rfb_handshake=ok" in connect
    assert "rfb_handshake=ok" in connect2
    assert "rfb_handshake=ok" in tls
    assert "denied=ok" in denied or "close_code=1008" in denied
