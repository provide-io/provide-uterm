#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Headed Playwright screen recording: browser resume with OBVIOUS on-screen proof.

Prior captures looked empty because the terminal pane has no shell worker and
the real status text lives in a small shadow-DOM toolbar. This recorder:

  * Mirrors #statustext into a huge fixed HUD (always readable in video)
  * Writes the same status into the xterm when possible
  * Holds several seconds on \"Resumed\" so the frame is not a one-frame flash
  * Crops a zoom shot of the HUD

Prereqs: wrangler on BASE_URL + CF_E2E_JWT (or /tmp/cf_e2e_jwt.env).

Outputs: artifacts/cf-resume-proof/<ts>/  (gitignored)
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Injected into every page: giant HUD + status mirror + term writer.
_HUD_JS = r"""
(() => {
  function ensureHud() {
    let hud = document.getElementById('resume-proof-hud');
    if (hud) return hud;
    hud = document.createElement('div');
    hud.id = 'resume-proof-hud';
    hud.style.cssText = [
      'position:fixed', 'inset:0', 'z-index:2147483646',
      'display:flex', 'flex-direction:column', 'justify-content:center', 'align-items:center',
      'pointer-events:none', 'font-family:ui-monospace,SFMono-Regular,Menlo,monospace',
      'background:rgba(0,0,0,0.55)', 'color:#f8fafc', 'padding:24px', 'text-align:center',
    ].join(';');
    hud.innerHTML = `
      <div id="resume-proof-phase" style="font-size:18px;opacity:.85;margin-bottom:12px"></div>
      <div id="resume-proof-status" style="font-size:72px;font-weight:800;letter-spacing:.02em;
        padding:24px 48px;border-radius:16px;border:4px solid #64748b;background:#0f172a;
        min-width:60%;box-shadow:0 0 0 1px #000">…</div>
      <div id="resume-proof-detail" style="font-size:16px;margin-top:18px;opacity:.9;max-width:90%"></div>
    `;
    document.documentElement.appendChild(hud);
    return hud;
  }
  function paint(status, phase, detail, hot) {
    ensureHud();
    const s = document.getElementById('resume-proof-status');
    const p = document.getElementById('resume-proof-phase');
    const d = document.getElementById('resume-proof-detail');
    if (p) p.textContent = phase || '';
    if (d) d.textContent = detail || '';
    if (s) {
      s.textContent = status || '(no status yet)';
      if (hot) {
        s.style.borderColor = '#22c55e';
        s.style.color = '#bbf7d0';
        s.style.background = '#052e16';
        s.style.fontSize = '96px';
      } else {
        s.style.borderColor = '#64748b';
        s.style.color = '#f8fafc';
        s.style.background = '#0f172a';
        s.style.fontSize = '72px';
      }
    }
  }
  function readStatus() {
    const el = document.querySelector('uterm-session');
    if (!el || !el.shadowRoot) return '';
    const s = el.shadowRoot.querySelector('#statustext');
    return s ? (s.textContent || '') : '';
  }
  function writeTerm(msg) {
    try {
      const el = document.querySelector('uterm-session');
      const term = el && (el.terminal || el._hijackState?.term);
      if (term && typeof term.writeln === 'function') {
        term.writeln('');
        term.writeln('\x1b[1;32m══════════════════════════════════════\x1b[0m');
        term.writeln('\x1b[1;32m  ' + msg + '\x1b[0m');
        term.writeln('\x1b[1;32m══════════════════════════════════════\x1b[0m');
        term.writeln('');
      }
    } catch (e) { /* ignore */ }
  }
  window.__proof = { paint, readStatus, writeTerm, ensureHud };
  // keep HUD on top if SPA mutates body
  const mo = new MutationObserver(() => ensureHud());
  if (document.body) mo.observe(document.body, { childList: true });
  else document.addEventListener('DOMContentLoaded', () => mo.observe(document.body, { childList: true }));
})();
"""


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
    host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]

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
        page.add_init_script(_HUD_JS)

        def status_text() -> str:
            return page.evaluate("() => window.__proof ? window.__proof.readStatus() : ''")

        def paint(status: str, phase: str, detail: str = "", *, hot: bool = False) -> None:
            page.evaluate(
                """([status, phase, detail, hot]) => {
                  if (window.__proof) window.__proof.paint(status, phase, detail, hot);
                }""",
                [status, phase, detail, hot],
            )

        def ensure_hud() -> None:
            page.evaluate("() => { if (window.__proof) window.__proof.ensureHud(); }")

        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        ensure_hud()
        paint("…", "1/3 First connect", "waiting for hello + resume_token")
        page.wait_for_timeout(800)

        for _ in range(50):
            st = status_text()
            if st:
                paint(st, "1/3 First connect", "live status from #statustext")
            if st and "Connecting" not in st and "Failed" not in st:
                break
            page.wait_for_timeout(200)

        st1 = status_text()
        tok = page.evaluate(f"() => sessionStorage.getItem('uterm_resume_{worker_id}') || ''")
        paint(
            st1 or "(empty)",
            "1/3 Connected",
            f"resume token stored in sessionStorage (len={len(tok)})",
        )
        page.evaluate(
            """(msg) => { if (window.__proof) window.__proof.writeTerm(msg); }""",
            f"CONNECTED status={st1!s} token_len={len(tok)}",
        )
        page.wait_for_timeout(1500)  # hold for video
        page.screenshot(path=str(shots / "01_connected.png"), full_page=True)
        page.locator("#resume-proof-hud").screenshot(path=str(shots / "01_connected_hud.png"))
        print(f"step1 status={st1!r} token_len={len(tok)}")
        if not tok:
            page.screenshot(path=str(shots / "FAIL_no_token.png"), full_page=True)
            context.close()
            browser.close()
            raise SystemExit("no resume token in sessionStorage after connect")

        paint(st1 or "…", "2/3 Reloading", "sessionStorage keeps token → client sends type=resume")
        page.wait_for_timeout(1000)
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        # re-apply HUD after full reload
        page.evaluate(_HUD_JS)
        ensure_hud()
        paint("…", "2/3 Reconnected", "waiting for hello.resumed=true → status Resumed")

        resumed_seen = False
        for i in range(100):
            st = status_text()
            if st:
                hot = "Resumed" in st
                paint(
                    st,
                    "3/3 PROOF" if hot else "2/3 Reconnected",
                    "hello.resumed=true → UI status flash" if hot else "polling #statustext…",
                    hot=hot,
                )
            if "Resumed" in (st or ""):
                resumed_seen = True
                page.evaluate(
                    """(msg) => { if (window.__proof) window.__proof.writeTerm(msg); }""",
                    "★★★ SESSION RESUMED ★★★  (hello.resumed=true)",
                )
                # Hold long enough that a human watching the webm cannot miss it.
                for hold in range(8):
                    paint(
                        "Resumed",
                        f"3/3 PROOF  hold {hold + 1}/8s",
                        "status chrome + HUD + terminal line — hello.resumed=true",
                        hot=True,
                    )
                    if hold == 1:
                        page.screenshot(path=str(shots / "02_resumed.png"), full_page=True)
                        page.locator("#resume-proof-hud").screenshot(path=str(shots / "02_resumed_hud.png"))
                    page.wait_for_timeout(1000)
                print(f"step2 RESUMED at poll={i} status={st!r}")
                break
            page.wait_for_timeout(100)
        else:
            page.screenshot(path=str(shots / "FAIL_no_resumed.png"), full_page=True)
            st = status_text()
            print(f"FAIL: never saw Resumed; last status={st!r}")

        st3 = status_text()
        paint(st3 or "(empty)", "done", f"after flash hold — last status={st3!s}")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(shots / "03_after_flash.png"), full_page=True)
        print(f"step3 after_flash status={st3!r}")

        page_path = page.video.path() if page.video else None
        context.close()
        browser.close()

        videos = list(out.glob("*.webm"))
        if page_path:
            videos.append(Path(page_path))
        videos = sorted({v.resolve() for v in videos if v.exists()})
        # Friendly copy of the primary video
        if videos:
            friendly = out / "cf-resume-proof.webm"
            friendly.write_bytes(videos[0].read_bytes())
            videos = [friendly, *videos]

        proof = out / "PROOF.txt"
        proof.write_text(
            "\n".join(
                [
                    "CF browser resume VISUAL proof (large HUD)",
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
                    "Look at 02_resumed_hud.png — giant RESUMED label, not the empty terminal.",
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
