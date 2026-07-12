#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Text session recording of browser resume (status chrome + control frames).

Unlike the Playwright webm proof, this emits a human-readable transcript and a
JSONL event log of what the session *says* — hello/resume frames and the
visible status line (including the \"Resumed\" flash).

Outputs under ``artifacts/cf-resume-proof/<ts>-text/``:
  - session_transcript.txt  — timeline of status + frames
  - session.jsonl           — structured events (recording-store style)
  - PROOF.txt
"""

from __future__ import annotations

import json
import os
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


def _decode_control(raw: str) -> list[dict]:
    from provide.uterm.control_channel import ControlChunk, ControlFrameDecoder

    payload = raw
    if len(raw) > 9 and raw[8] == ":":
        payload = raw[9:]
    dec = ControlFrameDecoder()
    events = dec.feed(payload)
    events.extend(dec.finish())
    out: list[dict] = []
    for e in events:
        if isinstance(e, ControlChunk):
            out.append(e.control)
    if not out:
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            pass
    return out


def main() -> int:
    from playwright.sync_api import sync_playwright

    base = os.environ.get("REAL_CF_URL", os.environ.get("BASE_URL", "http://127.0.0.1:8989")).rstrip("/")
    jwt = _load_jwt()
    worker_id = os.environ.get("PROOF_WORKER_ID", f"proof-text-{int(time.time()) % 100000}")
    out = ROOT / "artifacts" / "cf-resume-proof" / f"{time.strftime('%Y%m%d-%H%M%S')}-text"
    out.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    lines: list[str] = []
    events: list[dict] = []

    def elapsed() -> float:
        return round(time.monotonic() - t0, 3)

    def log_line(kind: str, msg: str) -> None:
        ts = elapsed()
        line = f"[{ts:8.3f}s] {kind:12} {msg}"
        lines.append(line)
        print(line, flush=True)

    def emit(kind: str, **data: object) -> None:
        rec = {"ts": elapsed(), "event": kind, **data}
        events.append(rec)
        if kind == "status":
            log_line("STATUS", str(data.get("text", "")))
        elif kind == "hello":
            log_line(
                "HELLO",
                f"resumed={data.get('resumed')!r} token={(str(data.get('resume_token') or '')[:16] + '…') if data.get('resume_token') else None} "
                f"role={data.get('role')!r} worker_online={data.get('worker_online')!r}",
            )
        elif kind == "client_resume":
            tok = str(data.get("token") or "")
            log_line("CLIENT→", f"type=resume token={tok[:16]}…")
        elif kind == "phase":
            log_line("PHASE", str(data.get("name", "")))
        else:
            log_line(kind.upper(), json.dumps(data, default=str)[:200])

    host = base.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    url = f"{base}/hijack.html?worker={worker_id}"
    emit("phase", name=f"start base={base} worker={worker_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        context.add_cookies(
            [
                {
                    "name": "CF_Authorization",
                    "value": jwt,
                    "domain": host,
                    "path": "/",
                    "httpOnly": False,
                    "secure": base.startswith("https://"),
                    "sameSite": "Lax",
                }
            ]
        )
        page = context.new_page()

        def on_ws(ws) -> None:  # type: ignore[no-untyped-def]
            emit("phase", name=f"ws_open {ws.url}")

            def on_recv(payload: object) -> None:
                s = (
                    payload
                    if isinstance(payload, str)
                    else (
                        payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
                    )
                )
                for msg in _decode_control(s):
                    if msg.get("type") == "hello":
                        emit(
                            "hello",
                            resumed=msg.get("resumed"),
                            resume_token=msg.get("resume_token"),
                            role=msg.get("role"),
                            worker_online=msg.get("worker_online"),
                            resume_supported=msg.get("resume_supported"),
                        )
                    elif msg.get("type") == "hijack_state":
                        emit("hijack_state", hijacked=msg.get("hijacked"), owner=msg.get("owner"))

            def on_sent(payload: object) -> None:
                s = (
                    payload
                    if isinstance(payload, str)
                    else (
                        payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
                    )
                )
                for msg in _decode_control(s):
                    if msg.get("type") == "resume":
                        emit("client_resume", token=msg.get("token"))

            ws.on("framereceived", on_recv)
            ws.on("framesent", on_sent)

        page.on("websocket", on_ws)

        def poll_status() -> str:
            return page.evaluate(
                """() => {
                  const el = document.querySelector('uterm-session');
                  if (!el || !el.shadowRoot) return '';
                  const s = el.shadowRoot.querySelector('#statustext');
                  return s ? (s.textContent || '') : '';
                }"""
            )

        # --- connect ---
        emit("phase", name="navigate first connect")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        last = ""
        for _ in range(50):
            st = poll_status()
            if st and st != last:
                emit("status", text=st)
                last = st
            if st and "Connecting" not in st:
                break
            page.wait_for_timeout(100)
        tok = page.evaluate(f"() => sessionStorage.getItem('uterm_resume_{worker_id}') || ''")
        emit(
            "session_storage", key=f"uterm_resume_{worker_id}", token_len=len(tok), token_prefix=tok[:12] if tok else ""
        )
        if not tok:
            emit("phase", name="FAIL no resume token after first connect")
            context.close()
            browser.close()
            return 1

        page.wait_for_timeout(800)

        # --- reload = reconnect ---
        emit("phase", name="page reload (browser reconnect)")
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        resumed_seen = False
        last = ""
        for _ in range(80):
            st = poll_status()
            if st and st != last:
                emit("status", text=st)
                last = st
            if "Resumed" in (st or ""):
                resumed_seen = True
                emit("phase", name="PROOF: status text shows Resumed")
                # hold to capture flash duration in transcript
                page.wait_for_timeout(1500)
                st2 = poll_status()
                if st2 != last:
                    emit("status", text=st2)
                break
            page.wait_for_timeout(100)

        page.wait_for_timeout(1200)
        st_end = poll_status()
        if st_end and st_end != last:
            emit("status", text=st_end)
        emit("phase", name="done" if resumed_seen else "FAIL no Resumed status text")

        context.close()
        browser.close()

    # write artifacts
    transcript = out / "session_transcript.txt"
    transcript.write_text(
        "\n".join(
            [
                "provide-uterm CF browser resume — TEXT SESSION RECORDING",
                f"base={base}",
                f"worker_id={worker_id}",
                f"url={url}",
                "",
                *lines,
                "",
                f"RESULT: {'PASS — Resumed indicator observed in status text' if resumed_seen else 'FAIL'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    jsonl = out / "session.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": 0,
                    "event": "log_start",
                    "data": {
                        "feature": "cf_browser_resume",
                        "worker_id": worker_id,
                        "base": base,
                        "kind": "text_session_recording",
                    },
                }
            )
            + "\n"
        )
        for rec in events:
            f.write(json.dumps(rec, default=str) + "\n")
        f.write(
            json.dumps(
                {
                    "ts": elapsed(),
                    "event": "log_end",
                    "data": {"resumed_seen": resumed_seen, "pass": resumed_seen},
                }
            )
            + "\n"
        )

    proof = out / "PROOF.txt"
    proof.write_text(
        "\n".join(
            [
                "TEXT session recording — resume indicator",
                f"resumed_seen={resumed_seen}",
                f"transcript={transcript}",
                f"jsonl={jsonl}",
                "PASS" if resumed_seen else "FAIL",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(transcript.read_text(encoding="utf-8"))
    print(f"wrote {out}")
    return 0 if resumed_seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
