#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Realistic CF resume demo recording (terminal text + idle + typing + resume).

What the video shows (no giant proof HUD):

  1. Browser opens a session; a **worker** pushes real terminal text.
  2. Worker \"types\" more lines (streaming term frames).
  3. Idle wait so status can settle / feel like a timeout window.
  4. Browser reloads (drop) — brief reconnect.
  5. Session **resumes**: snapshot restored, status shows **Resumed**.
  6. Worker types again so you see the session is live after resume.

Requires:
  - wrangler on BASE_URL (default http://127.0.0.1:8989)
  - CF_E2E_JWT (or /tmp/cf_e2e_jwt.env)
  - CF_WORKER_BEARER_TOKEN matching worker WORKER_BEARER_TOKEN
    (or packages/provide-uterm-cloudflare/.dev.vars)

Outputs under artifacts/cf-resume-proof/<ts>-demo/ (gitignored).
"""

from __future__ import annotations

import os
import re
import threading
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
    raise SystemExit("CF_E2E_JWT not set")


def _load_bearer() -> str:
    b = os.environ.get("CF_WORKER_BEARER_TOKEN", "").strip()
    if b:
        return b
    dev = ROOT / "packages/provide-uterm-cloudflare/.dev.vars"
    if dev.is_file():
        for line in dev.read_text(encoding="utf-8").splitlines():
            if line.startswith("WORKER_BEARER_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("CF_WORKER_BEARER_TOKEN / .dev.vars missing")


class WorkerDriver:
    """Background worker WS that feeds terminal text into the DO."""

    def __init__(self, base: str, worker_id: str, bearer: str) -> None:
        self.base = base
        self.worker_id = worker_id
        self.bearer = bearer
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._send_queue: list[str] = []
        self._lock = threading.Lock()
        self._screen = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="cf-worker-driver", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError(self._error or "worker WS did not become ready")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def push_snapshot(self, screen: str) -> None:
        from provide.uterm.control_channel import encode_control_frame

        self._screen = screen
        frame = encode_control_frame({"type": "snapshot", "screen": screen, "cols": 80, "rows": 24, "ts": time.time()})
        with self._lock:
            self._send_queue.append(frame)

    def type_line(self, line: str, *, char_delay: float = 0.04) -> None:
        """Append a line to the screen and stream it as term data + final snapshot."""
        from provide.uterm.control_channel import encode_control_frame

        # Stream characters so the video shows typing.
        for ch in line:
            with self._lock:
                self._send_queue.append(encode_control_frame({"type": "term", "data": ch, "ts": time.time()}))
            time.sleep(char_delay)
        with self._lock:
            self._send_queue.append(encode_control_frame({"type": "term", "data": "\r\n", "ts": time.time()}))
        self._screen = (self._screen.rstrip("\n") + "\n" + line + "\n") if self._screen else (line + "\n")
        # Keep DO last_snapshot in sync for resume replay.
        self.push_snapshot(self._screen)

    def _run(self) -> None:
        import asyncio

        import websockets

        from provide.uterm.control_channel import encode_control_frame

        async def amain() -> None:
            ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")
            uri = f"{ws_base}/ws/worker/{self.worker_id}/term"
            try:
                async with websockets.connect(
                    uri,
                    additional_headers={"Authorization": f"Bearer {self.bearer}"},
                    open_timeout=15,
                ) as ws:
                    self._ready.set()
                    # Initial banner so the terminal is never blank.
                    intro = (
                        "╔══════════════════════════════════════╗\n"
                        "║  provide-uterm  ·  session live      ║\n"
                        "╚══════════════════════════════════════╝\n"
                        "\n"
                        "user@edge:~$ whoami\n"
                        "operator\n"
                        "user@edge:~$ \n"
                    )
                    self._screen = intro
                    await ws.send(
                        encode_control_frame(
                            {"type": "snapshot", "screen": intro, "cols": 80, "rows": 24, "ts": time.time()}
                        )
                    )
                    while not self._stop.is_set():
                        batch: list[str] = []
                        with self._lock:
                            if self._send_queue:
                                batch = self._send_queue[:]
                                self._send_queue.clear()
                        for frame in batch:
                            await ws.send(frame)
                        await asyncio.sleep(0.05)
            except Exception as exc:
                self._error = str(exc)
                self._ready.set()

        asyncio.run(amain())


def main() -> int:
    from playwright.sync_api import sync_playwright

    base = os.environ.get("REAL_CF_URL", os.environ.get("BASE_URL", "http://127.0.0.1:8989")).rstrip("/")
    jwt = _load_jwt()
    bearer = _load_bearer()
    worker_id = os.environ.get("PROOF_WORKER_ID", f"demo-resume-{int(time.time()) % 100000}")
    out = ROOT / "artifacts" / "cf-resume-proof" / f"{time.strftime('%Y%m%d-%H%M%S')}-demo"
    out.mkdir(parents=True, exist_ok=True)
    shots = out / "screenshots"
    shots.mkdir(exist_ok=True)

    idle_s = float(os.environ.get("DEMO_IDLE_S", "4"))
    host = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0]
    url = f"{base}/hijack.html?worker={worker_id}"
    is_https = base.startswith("https://")

    print(f"base={base}")
    print(f"worker_id={worker_id}")
    print(f"out={out}")

    worker = WorkerDriver(base, worker_id, bearer)
    worker.start()
    print("worker online")

    try:
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

            def status_text() -> str:
                return page.evaluate(
                    """() => {
                      const el = document.querySelector('uterm-session');
                      if (!el || !el.shadowRoot) return '';
                      const s = el.shadowRoot.querySelector('#statustext');
                      return s ? (s.textContent || '') : '';
                    }"""
                )

            def term_has(text: str) -> bool:
                # xterm renders into canvas/DOM — use accessibility text + buffer if exposed.
                return page.evaluate(
                    """(needle) => {
                      const el = document.querySelector('uterm-session');
                      if (!el) return false;
                      const t = el.terminal || el._hijackState?.term;
                      if (t && t.buffer && t.buffer.active) {
                        const buf = t.buffer.active;
                        let acc = '';
                        for (let i = 0; i < buf.length; i++) {
                          const line = buf.getLine(i);
                          if (line) acc += line.translateToString(true) + '\\n';
                        }
                        if (acc.includes(needle)) return true;
                      }
                      // Fallback: page text
                      return (document.body.innerText || '').includes(needle);
                    }""",
                    text,
                )

            # --- 1. Open browser, wait for session UI + first snapshot ---
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            for _ in range(40):
                st = status_text()
                if st and "Connecting" not in st:
                    break
                page.wait_for_timeout(200)
            # Worker already sent intro snapshot; re-push so late joiners get it.
            worker.push_snapshot(worker._screen)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shots / "01_terminal_text.png"), full_page=True)
            print(f"step1 status={status_text()!r}")

            # --- 2. Someone typing ---
            worker.type_line("date")
            worker.type_line("Sat Jul 12 11:30:00 UTC 2026")
            worker.type_line("user@edge:~$ uptime")
            worker.type_line(" 11:30:01 up 3 days,  2:14,  1 user,  load average: 0.08")
            worker.type_line("user@edge:~$ ")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(shots / "02_after_typing.png"), full_page=True)
            print("step2 typed shell-like activity")

            # --- 3. Idle / timeout feel ---
            print(f"step3 idle {idle_s}s (watching session…)")
            t_end = time.time() + idle_s
            while time.time() < t_end:
                page.wait_for_timeout(400)
            page.screenshot(path=str(shots / "03_idle.png"), full_page=True)

            tok = page.evaluate(f"() => sessionStorage.getItem('uterm_resume_{worker_id}') || ''")
            if not tok:
                page.screenshot(path=str(shots / "FAIL_no_token.png"), full_page=True)
                raise SystemExit("no resume token — cannot demo resume")

            # --- 4. Drop + resume ---
            print("step4 reload (drop)…")
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            # Re-push snapshot so resume path has content (also stored in DO).
            worker.push_snapshot(worker._screen)

            resumed_seen = False
            for _i in range(80):
                st = status_text()
                if "Resumed" in (st or ""):
                    resumed_seen = True
                    page.screenshot(path=str(shots / "04_resumed.png"), full_page=True)
                    # Hold so video shows the real status chrome "Resumed"
                    page.wait_for_timeout(2200)
                    print(f"step4 RESUMED status={st!r}")
                    break
                page.wait_for_timeout(100)
            if not resumed_seen:
                page.screenshot(path=str(shots / "FAIL_no_resumed.png"), full_page=True)
                print(f"WARN: status never showed Resumed (last={status_text()!r})")

            # After resume, typing continues — proves live session, not a static page.
            worker.type_line("# … connection dropped, session resumed …")
            worker.type_line("echo still here after resume")
            worker.type_line("still here after resume")
            worker.type_line("user@edge:~$ ")
            page.wait_for_timeout(2000)
            page.screenshot(path=str(shots / "05_typing_after_resume.png"), full_page=True)
            print("step5 typing after resume")

            st_end = status_text()
            page.wait_for_timeout(800)
            page_path = page.video.path() if page.video else None
            context.close()
            browser.close()

            videos = list(out.glob("*.webm"))
            if page_path:
                videos.append(Path(page_path))
            videos = sorted({v.resolve() for v in videos if v.exists()})
            if videos:
                friendly = out / "cf-resume-demo.webm"
                friendly.write_bytes(videos[0].read_bytes())

            proof = out / "PROOF.txt"
            proof.write_text(
                "\n".join(
                    [
                        "CF resume DEMO (terminal text + idle + typing + resume)",
                        f"base={base}",
                        f"worker_id={worker_id}",
                        f"url={url}",
                        f"token_len={len(tok)}",
                        f"resumed_seen={resumed_seen}",
                        f"status_end={st_end!r}",
                        f"video={out / 'cf-resume-demo.webm'}",
                        f"screenshots={sorted(p.name for p in shots.glob('*.png'))}",
                        "",
                        "Storyboard:",
                        "  01_terminal_text.png     — session live with shell text",
                        "  02_after_typing.png      — more lines appeared (worker typing)",
                        "  03_idle.png              — idle wait",
                        "  04_resumed.png           — after reload, status Resumed + text restored",
                        "  05_typing_after_resume.png — activity continues post-resume",
                        "",
                        "PASS" if resumed_seen else "PARTIAL — check video; resume flag may have been brief",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            print(proof.read_text(encoding="utf-8"))
            return 0 if resumed_seen else 1
    finally:
        worker.stop()


if __name__ == "__main__":
    raise SystemExit(main())
