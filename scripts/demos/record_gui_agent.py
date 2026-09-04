#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: an agent drives a real desktop, and a human watches it happen.

This was a cast until 2026-09-04, because ``gui/attach`` answered 501 for
``rfb`` and the only console the seven ``gui_*`` tools could reach was
``MemoryGraphicalSession`` -- a framebuffer that paints one white pixel per
click and ignores keys entirely. There was nothing to film.
``server/rfb_session.py`` changed that, so this runs against the
``uterm-test-vnc`` lab now.

Two views of one console, which is the point:

* The **agent** works through REST -- ``gui/attach`` onto the lab's RFB target,
  then screenshot, click, type, key, drag, screenshot, release. Pull-based, so
  it does not depend on anything streaming smoothly.
* The **browser** sits on the first-party ``vnc.html``, watching that same
  desktop through the human VNC relay while those calls land.

The lease is what ties them together: the agent holds it, and the RFB input
filter drops injected input from anyone who does not.

Requires Docker. Without it this raises and the orchestrator records a [SKIP]
rather than publishing a feature with no video.
"""

from __future__ import annotations

import base64
import subprocess
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from scripts.demos import (
    BASE_OUT,
    BrowserStep,
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

FEATURE = "gui_agent"
DESCRIPTION = (
    "An agent takes the graphical lease, screenshots a live desktop, clicks and types on it, then hands it back"
)
TITLE = "Agent Drives a Desktop"
SUBTITLE = "The same lease that arbitrates a PTY arbitrates a screen"
HIGHLIGHT_START_S: float = 2.0
HIGHLIGHT_DURATION_S: float = 12.0

#: Its own container and ports, so a demo run never tears down a lab somebody
#: is using interactively.
LAB_NAME = "uterm-demo-gui-agent"
LAB_HOST_PLAIN = 25902
LAB_HOST_TLS = 25903

#: Where the agent works: inside the lab's xterm, which echoes what it is
#: sent, so the screenshot afterwards shows the effect rather than an inert
#: buffer. The drag stays within the window so it selects visible text.
_CLICK_XY = (300, 200)
_TYPED_TEXT = "echo 'an agent typed this'"
_DRAG = (60, 96, 420, 96)


def _agent_call(name: str, detail: str = "") -> None:
    """Print a tool call the way an MCP harness shows one."""
    info(f"  -> {name}({detail})" if detail else f"  -> {name}()")


def _screenshot_summary(payload: object) -> str:
    """Describe a screenshot response without dumping the base64 blob.

    The key is screenshot (rest_gui.py). An earlier version guessed at
    png_base64/image and printed "no image in response" for every
    successful capture.
    """
    if not isinstance(payload, dict):
        return "unrecognised response"
    raw = payload.get("screenshot") or ""
    if not isinstance(raw, str) or not raw:
        return "no image in response"
    return f"{len(base64.b64decode(raw))} bytes of PNG"


def _stand_up(vnc_demo: Any, prove: Any) -> tuple[str, Any, str]:
    """Start the lab and a server that knows about it; return the held lease."""
    info(f"Starting the {vnc_demo.vnc_lab.IMAGE_NAME} lab desktop...")
    port = vnc_demo._free_port()
    base_url = f"http://127.0.0.1:{port}"
    root = vnc_demo._repo_root()
    config = root / "demo" / f".{FEATURE}-server.toml"

    vnc_demo.write_demo_server_config(
        config, host="0.0.0.0", port=port, plain_port=LAB_HOST_PLAIN, tls_port=LAB_HOST_TLS
    )
    demo_url = f"http://{vnc_demo._HOST_FROM_DOCKER}:{port}/_terminal/terminal.html?worker_id={prove.SERVER_SESSION}"
    # xterm, not the browser console: the console renders the same whatever is
    # typed at the X server, so it cannot show what the agent does. Measured —
    # x11vnc received every event and zero of 1,920,000 framebuffer bytes moved.
    vnc_demo.start_lab_with_demo_url(
        name=LAB_NAME, demo_url=demo_url, host_plain=LAB_HOST_PLAIN, host_tls=LAB_HOST_TLS, xterm=True
    )
    kv("lab", f"{LAB_NAME} :{LAB_HOST_PLAIN} (plain)")

    server = None
    try:
        server = prove.start_server(root=root, config=config, log_path=root / "demo" / f".{FEATURE}-server.log")
        # Readiness first: wait_worker_and_acquire polls for a worker and treats
        # a refused connection as a hard error rather than "not up yet".
        prove.wait_http(f"{base_url}/readyz", timeout=45.0)
        hijack_id = prove.wait_worker_and_acquire(base_url)
        kv("lease", hijack_id)

        status, body = vnc_demo.http_json(
            "POST",
            f"{base_url}/worker/{prove.SERVER_SESSION}/gui/attach",
            headers=prove.ADMIN_HEADERS,
            body={"target_id": prove.TARGET_PLAIN},
        )
        if status != 200:
            # A 501 here would mean the RFB client regressed to the memory-only
            # state this demo was written around. Say so rather than film it.
            raise RuntimeError(f"gui/attach {prove.TARGET_PLAIN} = {status} {body!r}")
        kv("attached", f"{prove.TARGET_PLAIN} (rfb, status 200)")
    except BaseException:
        # This function started the lab and the server, so it owns tearing them
        # down; the caller's finally only arms once it has returned.
        _tear_down(prove, server)
        raise
    return base_url, server, hijack_id


def _tear_down(prove: Any, server: Any) -> None:
    """Stop the server and remove the lab container."""
    if server is not None:
        prove.stop_server(server)
    subprocess.run(["docker", "rm", "-f", LAB_NAME], capture_output=True, check=False, timeout=30)


def _drive_tools(vnc_demo: Any, prove: Any, base_url: str, hijack_id: str, *, narrate: bool) -> None:
    """Run the agent's sequence against the attached console."""
    lease = f"{base_url}/worker/{prove.SERVER_SESSION}/hijack/{hijack_id}/gui"

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, object]:
        return vnc_demo.http_json(method, f"{lease}{path}", headers=prove.ADMIN_HEADERS, body=body)

    def report(status: int, payload: object, label: str) -> None:
        if not narrate:
            return
        if status == 200:
            ok(f"{label} -> {_screenshot_summary(payload)}")
        else:
            warn(f"{label} refused -> {status}")

    if narrate:
        _agent_call("gui_screenshot", "before")
    status, payload = call("GET", "/screenshot")
    report(status, payload, "gui_screenshot")

    x, y = _CLICK_XY
    if narrate:
        _agent_call("gui_click", f"x={x}, y={y}, button='left'")
    call("POST", "/click", {"x": x, "y": y, "button": "left"})
    time.sleep(0.6)

    if narrate:
        _agent_call("gui_type", f"text={_TYPED_TEXT!r}")
    call("POST", "/type", {"text": _TYPED_TEXT})
    # The terminal echoes as it receives, so give the repaint time to land in
    # the recording rather than after it.
    time.sleep(1.5)

    if narrate:
        _agent_call("gui_key", "key_name='Return'")
    call("POST", "/key", {"key_name": "Return"})
    time.sleep(1.5)

    sx, sy, ex, ey = _DRAG
    if narrate:
        _agent_call("gui_drag", f"start=({sx},{sy}) end=({ex},{ey})")
    call("POST", "/drag", {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey})
    time.sleep(0.8)

    if narrate:
        _agent_call("gui_screenshot", "after")
    status, payload = call("GET", "/screenshot")
    report(status, payload, "gui_screenshot")


def run_terminal_demo() -> None:
    """Narrate the agent's sequence against the lab console."""
    from scripts import prove_uterm_vnc_console as prove
    from scripts import record_uterm_vnc_demo_video as vnc_demo

    banner(DESCRIPTION)
    base_url, server, hijack_id = _stand_up(vnc_demo, prove)
    try:
        info("The console is a real X desktop over RFB, not a stub framebuffer.")
        _drive_tools(vnc_demo, prove, base_url, hijack_id, narrate=True)

        _agent_call("gui_hijack_release")
        vnc_demo.http_json(
            "POST",
            f"{base_url}/worker/{prove.SERVER_SESSION}/hijack/{hijack_id}/release",
            headers=prove.ADMIN_HEADERS,
            body={},
        )
        ok("lease released -- the desktop is arbitrable again")
        info("Without the lease those injections are dropped: the RFB input filter")
        info("gates KeyEvent / PointerEvent / ClientCutText and fails closed.")
    finally:
        _tear_down(prove, server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record the narration as a cast and the desktop as video."""
    feat_dir = out_dir(FEATURE, base_out)
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    from scripts import prove_uterm_vnc_console as prove
    from scripts import record_uterm_vnc_demo_video as vnc_demo

    base_url, server, hijack_id = _stand_up(vnc_demo, prove)
    try:
        console = (
            f"/_terminal/vnc.html?worker_id={prove.SERVER_SESSION}&hijack_id={hijack_id}&target_id={prove.TARGET_PLAIN}"
        )

        def connect_console(page: Any) -> None:
            """Press Connect if the page is waiting for it.

            The button is disabled once a session is live, so clicking
            unconditionally waits out Playwright's timeout on exactly the runs
            where the connection already succeeded.
            """
            button = page.locator("#vnc-connect")
            if button.is_enabled(timeout=5_000):
                button.click(timeout=5_000)

        steps: list[BrowserStep] = [
            (console, 1.5, None),
            (connect_console, 5.0, "01-console-attached.png"),
            # The agent works while the browser is recording. That is the shot.
            (lambda _page: _drive_tools(vnc_demo, prove, base_url, hijack_id, narrate=False), 2.0, None),
            (None, 2.0, "02-after-agent-input.png"),
        ]
        # The relay socket is refused 401 without these: this server runs header
        # auth, and a browser WebSocket cannot carry custom headers.
        # The principal must be the one holding the lease. The relay refuses a
        # viewer who does not own it -- "hijack lease not owned by caller" --
        # which is this demo's own subject matter enforcing itself.
        principal = [
            {
                "name": "uterm_principal",
                "value": prove.ADMIN_HEADERS["x-uterm-principal"],
                "domain": "127.0.0.1",
                "path": "/",
            },
            {"name": "uterm_role", "value": prove.ADMIN_HEADERS["x-uterm-role"], "domain": "127.0.0.1", "path": "/"},
            {
                "name": "uterm_tenant",
                "value": prove.ADMIN_HEADERS["x-uterm-tenant"],
                "domain": "127.0.0.1",
                "path": "/",
            },
        ]
        mp4_path = browser_record(base_url, steps, feat_dir, cookies=principal)
    finally:
        _tear_down(prove, server)

    highlight = trim_clip(mp4_path, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S) if mp4_path else None
    return {"cast": cast_path, "mp4": mp4_path, "highlight": highlight}


if __name__ == "__main__":
    run_terminal_demo()
