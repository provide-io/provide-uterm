#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Headed Playwright screen recording: browser resume → \"Resumed\" status flash.

Prereqs:
  - Local wrangler on BASE_URL (default http://127.0.0.1:8989) with JWT harness, OR
  - REAL_CF_URL + CF_E2E_JWT (+ Access headers if needed)
  - CF_E2E_JWT in env or /tmp/cf_e2e_jwt.env

Outputs under ``artifacts/cf-resume-proof/<timestamp>/``:
  - video (webm)
  - screenshots: 01_connected.png, 02_resumed.png, 03_after_flash.png
  - PROOF.txt summary
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_jwt() -> str:
    jwt = os.environ.get("CF_E2E_JWT", "").strip()
    if jwt:
        return jwt
    env_path = Path("/tmp/cf_e2e_jwt.env")
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CF_E2E_JWT="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("CF_E2E_JWT not set (and /tmp/cf_e2e_jwt.env missing)")


def main() -> int:
    from playwright.sync_api import sync_playwright

    base = os.environ.get("REAL_CF_URL", os.environ.get("BASE_URL", "http://127.0.0.1:8989")).rstrip("/")
    jwt = _load_jwt()
    worker_id = os.environ.get("PROOF_WORKER_ID", f"proof-resume-{int(time.time()) % 100000}")
    out = ROOT / "artifacts" / "cf-resume-proof" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    shots = out / "screenshots"
    shots.mkdir(exist_ok=True)

    is_https = base.startswith("https://")
    url = f"{base}/hijack.html?worker={worker_id}"

    print(f"base={base}")
    print(f"worker_id={worker_id}")
    print(f"out={out}")
    print(f"url={url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=os.environ.get("HEADLESS", "0") == "1")
        context = browser.new_context(
            record_video_dir=str(out),
            record_video_size={"width": 1280, "height": 800},
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        # CF_Authorization cookie is how browser WS upgrades authenticate.
        host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
        context.add_cookies(
            [
                {
                    "name": "CF_Authorization",
                    "value": jwt,
                    "domain": host,
                    "path": "/",
                    "httpOnly": False,
                    "secure": is_https,
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()

        # Banner so the recording is self-explanatory.
        page.add_init_script(
            """
            (() => {
              const ban = document.createElement('div');
              ban.id = 'resume-proof-banner';
              ban.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;padding:10px 16px;'
                + 'font:600 14px/1.4 system-ui,sans-serif;background:#111827;color:#f9fafb;'
                + 'border-bottom:2px solid #22c55e';
              ban.textContent = 'CF resume proof: waiting for first connect…';
              document.addEventListener('DOMContentLoaded', () => document.body.prepend(ban));
              window.__setProofBanner = (t) => {
                const el = document.getElementById('resume-proof-banner');
                if (el) el.textContent = t;
              };
            })();
            """
        )

        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)

        # Wait for uterm-session status (shadow DOM).
        def status_text() -> str:
            return page.evaluate(
                """() => {
                  const el = document.querySelector('uterm-session');
                  if (!el || !el.shadowRoot) return '';
                  const s = el.shadowRoot.querySelector('#statustext');
                  return s ? (s.textContent || '') : '';
                }"""
            )

        # First connect
        for _ in range(40):
            st = status_text()
            if st and "Connecting" not in st and "Failed" not in st:
                break
            page.wait_for_timeout(250)
        st1 = status_text()
        page.evaluate(
            f"() => window.__setProofBanner && window.__setProofBanner("
            f"'1/3 Connected — status: {st1!s} — resume token in sessionStorage')"
        )
        # Expose token presence for the banner
        tok = page.evaluate(f"() => sessionStorage.getItem('uterm_resume_{worker_id}') || ''")
        page.evaluate(
            f"() => window.__setProofBanner && window.__setProofBanner("
            f"'1/3 Connected | status={st1!s} | token_len={len(tok)}')"
        )
        page.screenshot(path=str(shots / "01_connected.png"), full_page=True)
        print(f"step1 status={st1!r} token_len={len(tok)}")
        if not tok:
            page.screenshot(path=str(shots / "FAIL_no_token.png"), full_page=True)
            context.close()
            browser.close()
            raise SystemExit("no resume token in sessionStorage after connect")

        page.wait_for_timeout(1200)

        # Full page reload = real browser reconnect; sessionStorage keeps resume token.
        page.evaluate(
            "() => window.__setProofBanner && window.__setProofBanner("
            "'2/3 Reloading page — client will send type=resume with stored token…')"
        )
        page.wait_for_timeout(600)
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        # Re-inject banner after reload (init script only runs on first navigation).
        page.evaluate(
            """() => {
              if (document.getElementById('resume-proof-banner')) return;
              const ban = document.createElement('div');
              ban.id = 'resume-proof-banner';
              ban.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;padding:10px 16px;'
                + 'font:600 14px/1.4 system-ui,sans-serif;background:#111827;color:#f9fafb;'
                + 'border-bottom:2px solid #22c55e';
              ban.textContent = '2/3 Reloaded — waiting for hello.resumed…';
              document.body.prepend(ban);
              window.__setProofBanner = (t) => {
                const el = document.getElementById('resume-proof-banner');
                if (el) el.textContent = t;
              };
            }"""
        )

        resumed_seen = False
        for i in range(80):
            st = status_text()
            if "Resumed" in (st or ""):
                resumed_seen = True
                page.evaluate(
                    "() => window.__setProofBanner && window.__setProofBanner("
                    "'3/3 PROOF: status shows Resumed — hello.resumed=true')"
                )
                page.screenshot(path=str(shots / "02_resumed.png"), full_page=True)
                print(f"step2 RESUMED at poll={i} status={st!r}")
                break
            page.wait_for_timeout(100)
        else:
            page.screenshot(path=str(shots / "FAIL_no_resumed.png"), full_page=True)
            st = status_text()
            tok2 = page.evaluate(f"() => sessionStorage.getItem('uterm_resume_{worker_id}') || ''")
            print(f"FAIL: never saw Resumed; last status={st!r} token_still={bool(tok2)}")

        # Hold so the flash is visible in the video.
        page.wait_for_timeout(2200)
        st3 = status_text()
        page.evaluate(
            f"() => window.__setProofBanner && window.__setProofBanner("
            f"'done — after flash status={st3!s} (expected Connected/Waking/watching)')"
        )
        page.screenshot(path=str(shots / "03_after_flash.png"), full_page=True)
        print(f"step3 after_flash status={st3!r}")

        page.wait_for_timeout(800)
        page_path = page.video.path() if page.video else None
        context.close()
        browser.close()

        # Playwright finalizes video after context close.
        videos = list(out.glob("*.webm"))
        if page_path:
            videos.append(Path(page_path))
        videos = sorted({v.resolve() for v in videos if v.exists()})
        proof = out / "PROOF.txt"
        proof.write_text(
            "\n".join(
                [
                    "CF browser resume visual proof",
                    f"base={base}",
                    f"worker_id={worker_id}",
                    f"url={url}",
                    f"status_connected={st1!r}",
                    f"token_len={len(tok)}",
                    f"resumed_seen={resumed_seen}",
                    f"status_after_flash={st3!r}",
                    f"videos={[str(v) for v in videos]}",
                    f"screenshots={sorted(p.name for p in shots.glob('*.png'))}",
                    "",
                    "PASS" if resumed_seen else "FAIL — Resumed status never appeared",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(proof.read_text(encoding="utf-8"))
        print(f"artifacts: {out}")
        return 0 if resumed_seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
