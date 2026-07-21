#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Docker VNC lab e2e: real RFB handshake + graphical browser navigation.

Builds/runs the committed ``docker/vnc-lab`` image (``uterm-test-vnc``) and
proves VNC is live via a real RFB protocol handshake (not just TCP open),
and that Chromium was launched to a concrete demo URL.

Run with::

    uv run pytest packages/provide-uterm/tests/e2e/test_docker_vnc_lab.py \\
        -m docker -v --no-cov --timeout=600
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

# Import the shipped proof module (real entry point — not reimplemented here).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import prove_vnc_lab as vnc_lab

_DEMO_URL = "https://example.com"
_CONTAINER = "uterm-test-vnc-pytest"
_docker_missing = shutil.which("docker") is None


def _evidence_dir(tmp_path: Path) -> Path:
    """Prefer harness SCRATCH/EVIDENCE_DIR when set; else pytest tmp."""
    for key in ("EVIDENCE_DIR", "SCRATCH"):
        val = os.environ.get(key)
        if val:
            p = Path(val) / "pytest-vnc-lab"
            p.mkdir(parents=True, exist_ok=True)
            return p
    return tmp_path


@pytest.fixture(scope="module")
def vnc_lab_image() -> str:
    """Build the lab image once per module (committed Dockerfile)."""
    if _docker_missing:
        pytest.skip("Docker not available")
    log = _REPO_ROOT / ".pytest_cache" / "vnc-image-build-pytest.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    vnc_lab.build_image(root=_REPO_ROOT, log_path=log)
    return vnc_lab.IMAGE_NAME


def test_dockerfile_and_entrypoint_exist() -> None:
    """Structural gate: lab assets are committed (works without Docker)."""
    lab = _REPO_ROOT / "docker" / "vnc-lab"
    assert (lab / "Dockerfile").is_file()
    assert (lab / "entrypoint.sh").is_file()
    text = (lab / "Dockerfile").read_text(encoding="utf-8")
    assert "x11vnc" in text or "tigervnc" in text
    assert "chromium" in text.lower() or "firefox" in text.lower()
    entry = (lab / "entrypoint.sh").read_text(encoding="utf-8")
    assert "DEMO_URL" in entry
    assert "browser-nav.log" in entry


def test_rfb_handshake_helper_rejects_non_rfb() -> None:
    """Unit-level: handshake parser rejects garbage (no Docker required)."""
    # Drive the real shipped rfb_handshake against a short-lived TCP peer that
    # speaks HTTP instead of RFB — proves the parser, not a reimplementation.
    import socket
    import threading

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve() -> None:
        conn, _ = srv.accept()
        try:
            conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=_serve, daemon=True).start()
    with pytest.raises(RuntimeError, match="invalid RFB"):
        vnc_lab.rfb_handshake("127.0.0.1", port, timeout=2.0)


@pytest.mark.docker
@pytest.mark.skipif(_docker_missing, reason="Docker not available")
def test_vnc_lab_rfb_and_browser_navigation(
    vnc_lab_image: str,
    tmp_path: Path,
) -> None:
    """Live path: container up → RFB handshake → navigation evidence."""
    assert vnc_lab_image == "uterm-test-vnc"
    evidence = _evidence_dir(tmp_path)
    # Two successive cycles (same image) — both must succeed.
    for run_index in (1, 2):
        vnc_lab.prove_once(
            root=_REPO_ROOT,
            evidence_dir=evidence,
            run_index=run_index,
            skip_build=True,  # fixture already built
            demo_url=_DEMO_URL,
            container_name=_CONTAINER,
        )
        suffix = "" if run_index == 1 else f"-{run_index}"
        connect = (evidence / f"vnc-connect{suffix}.log").read_text(encoding="utf-8")
        assert "rfb_handshake=ok" in connect
        assert "RFB " in connect
        browser = (evidence / f"browser-nav{suffix}.log").read_text(encoding="utf-8")
        assert _DEMO_URL in browser
        assert "browser_nav_url=" in browser
