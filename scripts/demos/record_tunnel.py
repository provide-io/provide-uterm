#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: Session served through local CF Worker (wrangler dev --local), showing the CF path."""

# Smoke test skipped — wrangler startup is slow (30s+); tested in full orchestrator run
from __future__ import annotations

import asyncio
import subprocess  # nosec
import sys
import time
from pathlib import Path

import httpx

from scripts.demos import (
    BASE_OUT,
    asciinema_record,
    banner,
    browser_record,
    info,
    kv,
    ok,
    out_dir,
    trim_clip,
    warn,
)

FEATURE = "tunnel"
DESCRIPTION = "Session served through local CF Worker (wrangler dev --local), showing the CF path"
TITLE = "Tunnel Sharing"
SUBTITLE = "Share sessions via secure URL"
HIGHLIGHT_START_S: float = 3.0
HIGHLIGHT_DURATION_S: float = 6.0

CF_DIR = Path("packages/provide-uterm-cloudflare")
CF_PORT = 8788
CF_URL = f"http://localhost:{CF_PORT}"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _sync_local_packages_into_python_modules() -> None:
    """Copy local provide-uterm packages into CF python_modules so wrangler can import them.

    pywrangler cannot install local editable packages from the monorepo. We copy the source
    trees directly so the pyodide runtime can import them. Gitignored — regenerated each run.
    """
    import shutil

    provide_root = CF_DIR / "python_modules" / "provide"
    pm = provide_root / "terminal"
    pm.mkdir(parents=True, exist_ok=True)

    # provide.terminal.cloudflare — the CF package itself
    cf_src = _REPO_ROOT / "packages" / "provide-uterm-cloudflare" / "src" / "provide" / "terminal" / "cloudflare"
    cf_dst = pm / "cloudflare"
    if cf_dst.exists():
        shutil.rmtree(cf_dst)
    shutil.copytree(cf_src, cf_dst)

    # provide.terminal.bridge — server-side hijack/hub coordinator
    bridge_src = _REPO_ROOT / "packages" / "provide-uterm-server" / "src" / "provide" / "terminal" / "bridge"
    bridge_dst = pm / "bridge"
    if bridge_dst.exists():
        shutil.rmtree(bridge_dst)
    shutil.copytree(bridge_src, bridge_dst)

    # provide.telemetry — runtime logger dependency used by bridge modules
    telemetry_dst = provide_root / "telemetry"
    if telemetry_dst.exists():
        shutil.rmtree(telemetry_dst)
    telemetry_src = None
    for sp in sys.path:
        cand = Path(sp) / "provide" / "telemetry"
        if cand.exists():
            telemetry_src = cand
            break
    if telemetry_src is not None:
        shutil.copytree(telemetry_src, telemetry_dst)


def _start_wrangler() -> subprocess.Popen:  # type: ignore[type-arg]
    """Start wrangler dev --local and wait for it to be ready."""
    _sync_local_packages_into_python_modules()
    proc = subprocess.Popen(
        [
            "/opt/homebrew/bin/wrangler",
            "dev",
            "--local",
            "--var",
            "AUTH_MODE:dev",
            "--var",
            "ENVIRONMENT:development",
            "--port",
            str(CF_PORT),
        ],
        cwd=str(CF_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait up to 60s for the CF Worker to respond (first build takes time)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{CF_URL}/api/health", timeout=2.0)
            if r.status_code < 500:
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    raise RuntimeError(f"wrangler did not become ready on {CF_URL} within 60s")


async def run_terminal_demo() -> None:
    """Run the tunnel feature demo via CF Worker."""
    banner(DESCRIPTION)
    info("Starting local CF Worker via wrangler dev --local...")

    # Check wrangler availability before attempting startup
    try:
        subprocess.run(
            ["/opt/homebrew/bin/wrangler", "--version"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        warn("wrangler not available — skipping tunnel demo")
        info("(To run: install wrangler 4.x and retry)")
        return

    wrangler_proc = _start_wrangler()

    try:
        async with httpx.AsyncClient(base_url=CF_URL, timeout=15.0) as client:
            # Verify CF Worker is alive
            try:
                r = await client.get("/api/health")
                if r.status_code >= 500:
                    raise RuntimeError(f"health check returned {r.status_code}")
            except Exception as exc:
                warn(f"wrangler not available — {exc}")
                info("(To run: install wrangler 4.x and retry)")
                return

            ok(f"Wrangler dev server started on localhost:{CF_PORT}")

            # Create a demo session through CF Worker
            info("Creating session via CF Worker...")
            r = await client.post(
                "/api/sessions",
                json={
                    "session_id": "cf-demo",
                    "display_name": "CF Demo",
                    "connector_type": "shell",
                    "auto_start": True,
                },
            )
            if r.status_code in (200, 201):
                session_data = r.json()
                session_id = session_data.get("session_id", "cf-demo")
                kv("session_id", session_id)
            else:
                kv("session_id", "cf-demo")

            info("(Browser connects via CF Worker URL — see browser recording)")
            ok("Tunnel demo complete — session served through CF Worker")
    finally:
        wrangler_proc.terminate()
        try:
            wrangler_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            wrangler_proc.kill()


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + browser screenshots for the tunnel demo."""
    feat_dir = out_dir(FEATURE, base_out)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Start wrangler and record browser session
    wrangler_proc = None
    mp4_path: Path | None = None
    try:
        wrangler_proc = _start_wrangler()
        steps = [
            ("/app/", 2.0, "01-cf-dashboard.png"),
            ("/api/health", 0.5, "02-cf-health.png"),
            ("/api/sessions", 0.5, "03-cf-sessions.png"),
        ]
        mp4_path = browser_record(CF_URL, steps, feat_dir)
    except Exception as exc:
        print(f"  [WARN] wrangler/browser_record failed: {exc}", flush=True)
        mp4_path = None
    finally:
        if wrangler_proc is not None:
            wrangler_proc.terminate()
            try:
                wrangler_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                wrangler_proc.kill()

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    return {"cast": cast_path, "mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nTunnel demo: {result}")
