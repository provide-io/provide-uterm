#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: ``uterm share`` — bearer-token share/control URLs for terminal sessions.

Records two simultaneous browser perspectives against the FastAPI server's
tunnel API:

  - **Control browser** opens the ``control_url`` (operator role: can type).
  - **Share browser**   opens the ``share_url``   (viewer role: read-only).

The operator types in their tab; the viewer's tab shows the output appearing
in real time, proving the tunnel feature works end-to-end against the
in-process server. No external Cloudflare Worker required — the CF backend
exposes the same ``/api/tunnels`` endpoint and could be swapped in by
pointing the recorder at a different base URL.

Tunnel tokens are stored as BLAKE2b hashes on the hub; the plain tokens
leave the create response exactly once and are embedded in the URLs the
browsers navigate to. See ``provide.uterm.tunnel.token_hash``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from pathlib import Path

from scripts.demos import (
    BASE_OUT,
    BrowserStep,
    asciinema_record,
    banner,
    info,
    kv,
    ok,
    out_dir,
    record_simultaneous_perspectives,
    send_to_session,
    start_server,
    stop_server,
    trim_clip,
    wait_for_terminal,
)

FEATURE = "tunnel"
DESCRIPTION = "Share a terminal session via bearer-token URLs (uterm share)"
TITLE = "Tunnel Sharing"
SUBTITLE = "Share URL is read-only, control URL grants operator role"
HIGHLIGHT_START_S: float = 2.0
HIGHLIGHT_DURATION_S: float = 6.0
# Multi-browser demo: control-side (operator) and share-side (viewer) videos.
# The control clip is the canonical highlight for the site catalog.
PRIMARY_VIDEO: str = "control_trim.mp4"


def _create_tunnel(base_url: str, ttl_s: int = 3600) -> dict[str, str]:
    """Create a tunnel and return the JSON response.

    The response carries ``share_url``, ``control_url``, ``worker_token``,
    and ``expires_at``. The plain tokens are present here exactly once;
    server-side storage retains only their BLAKE2b digests.
    """
    with httpx.Client(base_url=base_url, timeout=10.0) as http:
        r = http.post("/api/tunnels", json={"tunnel_type": "terminal", "ttl_s": ttl_s})
        r.raise_for_status()
        return dict(r.json())


def _start_echo_worker(base_url: str, tunnel_id: str, worker_token: str) -> threading.Thread:
    """Spawn a background thread that streams demo output through the tunnel.

    The thread opens a WebSocket to ``/tunnel/{tunnel_id}`` with the
    worker bearer token, sends a ``worker_hello`` control frame, then
    emits a series of ANSI-styled output frames so both share-side and
    control-side browsers have something visible to render. Returns the
    thread so the caller can join on shutdown.

    A daemon thread plus a self-terminating asyncio loop keeps the
    recorder simple: it just spawns the worker, opens browsers, and lets
    the worker live for the duration of the recording.
    """

    async def _run() -> None:
        import websockets  # local import keeps the demo script import-light
        from provide.uterm.tunnel.protocol import (
            CHANNEL_DATA,
            encode_control,
            encode_frame,
        )

        ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + f"/tunnel/{tunnel_id}"
        headers = {"Authorization": f"Bearer {worker_token}"}
        try:
            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                # Hello first, then a styled banner so both browsers have
                # an immediate visual that proves the tunnel routes worker
                # frames out to share/control viewers in real time.
                await ws.send(encode_control({"type": "worker_hello", "mode": "open"}))
                banner_bytes = (
                    b"\r\n\x1b[1;36m== Streaming through tunnel ==\x1b[0m\r\n"
                    b"\x1b[2mshare_url  \xe2\x86\x92 viewer role (read-only)\x1b[0m\r\n"
                    b"\x1b[2mcontrol_url \xe2\x86\x92 operator role\x1b[0m\r\n\r\n"
                )
                await ws.send(encode_frame(CHANNEL_DATA, banner_bytes))
                # Slow drip of styled output: gives the recording 10-15
                # seconds of visible activity so the highlight clip has
                # content to land on.
                colours = [b"\x1b[31m", b"\x1b[32m", b"\x1b[33m", b"\x1b[34m", b"\x1b[35m", b"\x1b[36m"]
                for i in range(30):
                    colour = colours[i % len(colours)]
                    line = colour + f"frame {i:02d}: tunnel still alive\x1b[0m\r\n".encode()
                    await ws.send(encode_frame(CHANNEL_DATA, line))
                    await asyncio.sleep(0.4)
        except Exception:
            pass

    def _target() -> None:
        try:
            asyncio.run(_run())
        except Exception:
            pass

    t = threading.Thread(target=_target, daemon=True, name="demo-tunnel-echo-worker")
    t.start()
    return t


async def run_terminal_demo() -> None:
    """Narrate the share/control URL flow in the asciinema cast.

    No browsers here — the visual proof lives in the browser recording.
    The cast captures the operator's perspective: the tunnel is minted,
    URLs are printed, role semantics explained, and the response is
    annotated to show the hashed-storage / one-shot-plaintext contract.
    """
    base_url, server = start_server()
    time.sleep(1.5)

    banner(DESCRIPTION)
    try:
        info("Pre-populating provide-shell so the tunnel session has context")
        send_to_session(base_url, "provide-shell", "echo 'session ready to share'\r", wait_s=0.6)
        send_to_session(base_url, "provide-shell", "uname -a\r", wait_s=0.6)

        info("POST /api/tunnels")
        tunnel = _create_tunnel(base_url, ttl_s=3600)
        tunnel_id = tunnel.get("tunnel_id", "")
        share_url = tunnel.get("share_url", "")
        control_url = tunnel.get("control_url", "")
        worker_token = tunnel.get("worker_token", "")
        expires_at = tunnel.get("expires_at")

        ok(f"tunnel_id={tunnel_id}")
        kv("share_url   (viewer)  ", share_url)
        kv("control_url (operator)", control_url)
        kv("worker_token (agent)  ", f"{worker_token[:12]}…  (full value only present in this response)")
        if expires_at is not None:
            kv("expires_at", str(expires_at))
        kv("at-rest storage", "BLAKE2b digests on app.state (plain tokens never return to the hub)")

        info("Run `uterm share --server <url>` (or any worker on /tunnel/{id}) to bridge a real PTY")
        info("Send teammates the share_url for read-only access; control_url grants operator role")
        ok("Tunnel demo complete")
    finally:
        stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record asciinema cast + two simultaneous browser videos (control, share).

    Both browsers stay live the whole time, watching a small in-process
    echo worker stream ANSI-colored output through ``/tunnel/{tid}``.
    The control browser navigates with the operator-role ``control_url``;
    the share browser navigates with the viewer-role ``share_url``. The
    later screenshots prove the same worker stream reaches both bearer
    URLs in real time.
    """
    feat_dir = out_dir(FEATURE, base_out)

    base_url, server = start_server()
    time.sleep(1.5)

    tunnel = _create_tunnel(base_url, ttl_s=3600)
    tunnel_id = tunnel.get("tunnel_id", "")
    worker_token = tunnel.get("worker_token", "")
    share_path = _strip_to_path(str(tunnel.get("share_url", "")))
    control_path = _strip_to_path(str(tunnel.get("control_url", "")))

    # Bring the tunnel session online before the browsers attach so their
    # initial screenshots show the streaming banner, not "waiting for worker".
    worker_thread = _start_echo_worker(base_url, tunnel_id, worker_token)
    time.sleep(0.8)

    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    perspectives: dict[str, list[BrowserStep]] = {
        "share": [
            (share_path, 0.5, None),  # navigate via share URL → viewer role
            (lambda p: wait_for_terminal(p), 1.5, "share-01-viewer-initial.png"),
            (None, 2.0, "share-02-worker-frames-visible.png"),
            (None, 2.0, "share-03-stream-progressing.png"),
        ],
        "control": [
            (control_path, 0.5, None),  # navigate via control URL → operator role
            (lambda p: wait_for_terminal(p), 1.5, "control-01-operator-initial.png"),
            (None, 2.0, "control-02-worker-frames-visible.png"),
            (None, 2.0, "control-03-stream-progressing.png"),
        ],
    }
    vids = record_simultaneous_perspectives(perspectives, base_url, feat_dir)

    stop_server(server)
    # Give the echo worker a moment to exit cleanly after the server closes.
    worker_thread.join(timeout=2.0)

    highlight = trim_clip(vids.get("control"), HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)
    kv("recorded tunnel_id", tunnel_id)
    return {
        "cast": cast_path,
        "share_mp4": vids.get("share"),
        "control_mp4": vids.get("control"),
        "highlight": highlight,
    }


def _strip_to_path(url: str) -> str:
    """Return path-plus-query from an absolute URL.

    ``record_simultaneous_perspectives`` navigates relative to a base URL;
    the tunnel API returns absolute URLs that already include the same
    host:port. We strip the scheme+host so navigation reuses the test
    server's base instead of opening a second connection.
    """
    if "://" not in url:
        return url
    return "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"


if __name__ == "__main__":
    import asyncio

    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nTunnel demo: {result}")
