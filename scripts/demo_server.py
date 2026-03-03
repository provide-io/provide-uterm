"""
Minimal demo server for manual + Playwright testing of the frontend widgets.

Serves terminal.html + hijack.html and provides:
  - /ws/terminal  → raw echo WS (for ProvideTerminal)
  - /ws/bot/{id}/term → mock hub WS (for ProvideHijack)
"""

import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from provide.terminal.fastapi import mount_terminal_ui

app = FastAPI()

# ── Serve static frontend at /terminal and / ─────────────────────────────────
mount_terminal_ui(app, path="/terminal")

# Also mount hijack.html etc at /hijack
import importlib.resources
from starlette.staticfiles import StaticFiles

frontend_path = importlib.resources.files("provide.terminal") / "frontend"
app.mount("/hijack", StaticFiles(directory=str(frontend_path), html=True), name="hijack-ui")


# ── Raw echo WS (terminal.html connects here) ─────────────────────────────────
@app.websocket("/ws")
async def ws_echo(ws: WebSocket) -> None:
    await ws.accept()
    await ws.send_text("\x1b[2J\x1b[H")  # clear screen
    await ws.send_text("\x1b[1;32mProvideTerminal echo server ready.\x1b[0m\r\n")
    await ws.send_text("\x1b[33mType anything — it echoes back.\x1b[0m\r\n$ ")
    try:
        while True:
            data = await ws.receive_text()
            await ws.send_text(data)
    except WebSocketDisconnect:
        pass


# ── Mock hub WS (hijack.html connects here) ───────────────────────────────────
@app.websocket("/ws/bot/{bot_id}/term")
async def ws_hub(ws: WebSocket, bot_id: str) -> None:
    await ws.accept()

    # hello
    await ws.send_text(json.dumps({
        "type": "hello",
        "bot_id": bot_id,
        "can_hijack": True,
        "hijacked": False,
        "hijacked_by_me": False,
    }))
    # hijack_state (not hijacked)
    await ws.send_text(json.dumps({
        "type": "hijack_state",
        "hijacked": False,
        "owner": None,
        "lease_expires_at": None,
    }))
    # snapshot with fake screen
    screen = (
        f"\x1b[1;34m[provide-terminal demo — bot: {bot_id}]\x1b[0m\n"
        "─" * 60 + "\n"
        "\x1b[32mStatus:\x1b[0m  Running\n"
        "\x1b[32mSector:\x1b[0m  42\n"
        "\x1b[32mCredits:\x1b[0m 1,234,567\n"
        "\n"
        "\x1b[33mAwaiting player input...\x1b[0m\n"
        "\n"
        "Command [TL=] (?=Help)? : \x1b[7m \x1b[0m"
    )
    await ws.send_text(json.dumps({
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": 30, "y": 8},
        "cols": 80,
        "rows": 25,
        "screen_hash": "abc123",
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "main_menu"},
        "ts": time.time(),
    }))

    hijacked = False
    hijacked_owner: str | None = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")

            if mtype == "snapshot_req":
                await ws.send_text(json.dumps({
                    "type": "snapshot",
                    "screen": screen,
                    "cursor": {"x": 30, "y": 8},
                    "cols": 80, "rows": 25,
                    "screen_hash": "abc123",
                    "cursor_at_end": True,
                    "has_trailing_space": False,
                    "prompt_detected": {"prompt_id": "main_menu"},
                    "ts": time.time(),
                }))

            elif mtype == "analyze_req":
                await ws.send_text(json.dumps({
                    "type": "analysis",
                    "formatted": f"[demo analysis for bot {bot_id}]\nprompt_id: main_menu\nscreen: 8 lines\ncursor: (30, 8)",
                    "ts": time.time(),
                }))

            elif mtype == "hijack_request":
                if not hijacked:
                    hijacked = True
                    hijacked_owner = "me"
                    await ws.send_text(json.dumps({
                        "type": "hijack_state",
                        "hijacked": True,
                        "owner": "me",
                        "lease_expires_at": time.time() + 90,
                    }))
                else:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": "Already hijacked.",
                    }))

            elif mtype == "hijack_step":
                if hijacked and hijacked_owner == "me":
                    await asyncio.sleep(0.2)
                    stepped_screen = screen + "\r\n\x1b[36m[stepped]\x1b[0m"
                    await ws.send_text(json.dumps({
                        "type": "snapshot",
                        "screen": stepped_screen,
                        "cursor": {"x": 0, "y": 9},
                        "cols": 80, "rows": 25,
                        "screen_hash": "def456",
                        "cursor_at_end": True,
                        "has_trailing_space": False,
                        "prompt_detected": {"prompt_id": "main_menu"},
                        "ts": time.time(),
                    }))

            elif mtype == "hijack_release":
                if hijacked and hijacked_owner == "me":
                    hijacked = False
                    hijacked_owner = None
                    await ws.send_text(json.dumps({
                        "type": "hijack_state",
                        "hijacked": False,
                        "owner": None,
                        "lease_expires_at": None,
                    }))

            elif mtype == "heartbeat":
                await ws.send_text(json.dumps({
                    "type": "heartbeat_ack",
                    "lease_expires_at": time.time() + 90,
                    "ts": time.time(),
                }))

            elif mtype == "input":
                data = msg.get("data", "")
                # echo input back as terminal data
                await ws.send_text(json.dumps({
                    "type": "term",
                    "data": data,
                    "ts": time.time(),
                }))

    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8742, log_level="warning")
