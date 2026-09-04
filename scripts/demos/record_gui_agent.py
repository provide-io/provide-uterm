#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: the graphical tool surface an agent drives, and the rules it obeys.

Cast, not video, and that is the honest shape for this one.

This runs against a ``memory`` target on purpose. ``MemoryGraphicalSession``'s
``inject_pointer`` sets a single white pixel and its ``inject_key`` is a
documented no-op, so there is nothing to film -- but every call is real, and the
contract around them is the point: the registry a target comes from, the tenant
scope that gates it, the lease every injection is charged against, and the
refusals.

``rfb`` targets attach for real now (``server/rfb_session.py``), so the demo also
shows the one thing an rfb target still cannot do: reach a console that is not
listening, which answers 502.

For a real desktop under a real lease see the ``graphical`` demo.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx2

if TYPE_CHECKING:
    from pathlib import Path

from scripts.demos import (
    BASE_OUT,
    asciinema_record,
    banner,
    info,
    kv,
    ok,
    out_dir,
    start_server,
    stop_server,
    warn,
)

FEATURE = "gui_agent"
DESCRIPTION = (
    "The seven graphical tools an agent drives: registry, tenant scope, hijack lease, and every refusal in between"
)
TITLE = "Graphical Tool Surface"
SUBTITLE = "What an agent may do to a desktop, and what it may not"
HIGHLIGHT_START_S: float = 0.0
HIGHLIGHT_DURATION_S: float = 0.0

#: No browser video: a memory framebuffer has nothing to film. See the module
#: docstring.
SITE_FORMAT = "cast"

#: Header auth, because the graphical registry derives its scope from
#: ``Principal.tenant_id`` and dev_token's stub principal carries no tenant.
_TENANT = "demo-tenant"
_HEADERS = {
    "x-uterm-principal": "agent-ada",
    "x-uterm-role": "admin",
    "x-uterm-tenant": _TENANT,
    "Content-Type": "application/json",
}

_WORKER = "provide-shell"


def _show(label: str, response: httpx2.Response) -> Any:
    """Print a call and its status the way an MCP harness would."""
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text
    if response.status_code < 400:
        ok(f"{label} → {response.status_code}")
    else:
        detail = body.get("error") or body.get("detail") if isinstance(body, dict) else body
        warn(f"{label} → {response.status_code} {detail}")
    return body


def run_terminal_demo() -> None:
    """Register a target, take the lease, drive the seven tools, be refused."""
    base_url, server = start_server(auth_mode="header")
    time.sleep(1.5)
    banner(DESCRIPTION)

    try:
        with httpx2.Client(base_url=base_url, timeout=30.0, headers=_HEADERS) as client:
            info("1. A target is a registry entry, not a connection string.")
            body = _show(
                "POST /api/graphical-targets",
                client.post(
                    "/api/graphical-targets",
                    json={
                        "display_name": "Demo Console",
                        "protocol": "memory",
                        "width": 800,
                        "height": 600,
                    },
                ),
            )
            target_id = body.get("target_id") if isinstance(body, dict) else None
            if isinstance(body, dict):
                kv(
                    "server-assigned id",
                    f"{target_id} ({body.get('protocol')}) {body.get('width')}x{body.get('height')}",
                )
                kv("secrets in response", "none" if "password" not in body else "LEAKED")

            info("2. The scope comes from the principal. A body cannot claim one.")
            _show(
                "POST /api/graphical-targets (tenant_id in body)",
                client.post(
                    "/api/graphical-targets",
                    json={"protocol": "memory", "tenant_id": "someone-else"},
                ),
            )

            info("3. Injection is charged against a hijack lease.")
            # REST hijack is refused 409 while the session is in "open" input
            # mode -- open means anyone may type, so there is no lease to take.
            _show(
                f"PATCH /api/sessions/{_WORKER} (input_mode=hijack)",
                client.patch(f"/api/sessions/{_WORKER}", json={"input_mode": "hijack"}),
            )
            time.sleep(0.5)
            lease = _show(
                f"POST /worker/{_WORKER}/hijack/acquire",
                client.post(f"/worker/{_WORKER}/hijack/acquire", json={"owner": "agent-ada", "lease_s": 120}),
            )
            hijack_id = lease.get("hijack_id") if isinstance(lease, dict) else None
            kv("lease", hijack_id or "none")

            _show(
                f"POST /worker/{_WORKER}/gui/attach",
                client.post(f"/worker/{_WORKER}/gui/attach", json={"target_id": target_id}),
            )

            info("4. The seven tools, in the order an agent uses them.")
            base = f"/worker/{_WORKER}/hijack/{hijack_id}/gui"
            _show("gui_screenshot", client.get(f"{base}/screenshot"))
            _show("gui_click", client.post(f"{base}/click", json={"x": 400, "y": 300, "button": "left"}))
            _show("gui_type", client.post(f"{base}/type", json={"text": "an agent typed this"}))
            _show("gui_key", client.post(f"{base}/key", json={"key_name": "Return"}))
            _show(
                "gui_drag", client.post(f"{base}/drag", json={"start_x": 10, "start_y": 10, "end_x": 700, "end_y": 500})
            )
            _show("gui_screenshot", client.get(f"{base}/screenshot"))

            info("5. The refusals are the contract.")
            _show(
                "gui_click (button='middle-ish')", client.post(f"{base}/click", json={"x": 1, "y": 1, "button": "nope"})
            )
            _show(
                "gui/attach (unregistered target)",
                client.post(f"/worker/{_WORKER}/gui/attach", json={"target_id": "gt-does-not-exist"}),
            )

            # An rfb target now attaches for real. What it cannot do is reach a
            # console that is not there: the registry entry is valid and the
            # dial fails, which is 502 rather than 501 or 422.
            rfb = _show(
                "POST /api/graphical-targets (protocol=rfb)",
                client.post(
                    "/api/graphical-targets",
                    json={
                        "display_name": "A Real Desktop",
                        "protocol": "rfb",
                        "endpoint": "rfb://127.0.0.1:1",
                    },
                ),
            )
            rfb_id = rfb.get("target_id") if isinstance(rfb, dict) else None
            _show(
                "gui/attach (protocol=rfb, unreachable console)",
                client.post(f"/worker/{_WORKER}/gui/attach", json={"target_id": rfb_id}),
            )

            _show(
                f"POST /worker/{_WORKER}/hijack/{hijack_id}/release",
                client.post(f"/worker/{_WORKER}/hijack/{hijack_id}/release", json={}),
            )
            _show("gui_click after release", client.post(f"{base}/click", json={"x": 400, "y": 300, "button": "left"}))

        ok("every tool answered, and an unreachable rfb console fails as a bad gateway, not a bad request")
    finally:
        stop_server(server)


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record the tool sequence as an asciinema cast. No video: see the docstring."""
    feat_dir = out_dir(FEATURE, base_out)
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")
    return {"cast": cast_path, "mp4": None}


if __name__ == "__main__":
    run_terminal_demo()
