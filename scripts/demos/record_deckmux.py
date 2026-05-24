#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo: 9-person production incident response with DeckMux collaborative presence.

Stages a realistic debugging scenario where a team assembles in a shared terminal,
scrolls through logs independently (edge indicator bars spread across the minimap),
and hands off control between team members (toast notifications, typing indicators).
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

from provide.uterm.bridge.hub import TermHub

from provide.uterm.deckmux._hub_mixin import DeckMuxMixin
from scripts.demos import (
    BASE_OUT,
    BrowserStep,
    asciinema_record,
    dev_bearer_headers,
    hstack_clips,
    out_dir,
    record_simultaneous_perspectives,
    start_server,
    stop_server,
    trim_clip,
    wait_connected,
    wait_for_presence_bar,
    wait_for_terminal,
)


class _DeckMuxTermHub(DeckMuxMixin, TermHub):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._deckmux_init()


FEATURE = "deckmux"
DESCRIPTION = "9-person production incident response with DeckMux collaborative presence"
TITLE = "DeckMux Presence"
SUBTITLE = "9 engineers debug a production incident together"
HIGHLIGHT_START_S: float = 100.0  # Act 4 handoff region
HIGHLIGHT_DURATION_S: float = 12.0
# Multi-browser demo: each engineer's perspective is recorded separately
# and stitched into composite.mp4. The composite is the catalog video.
PRIMARY_VIDEO: str = "composite_trim.mp4"

# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------

_CAST: list[dict[str, str]] = [
    {"name": "operator", "display": "Tanuki Tim", "role": "operator"},
    {"name": "bear_brody", "display": "Bear Brody", "role": "operator"},
    {"name": "crane_cara", "display": "Crane Cara", "role": "operator"},
    {"name": "falcon_finn", "display": "Falcon Finn", "role": "operator"},
    {"name": "lynx_liam", "display": "Lynx Liam", "role": "operator"},
    {"name": "wolf_willa", "display": "Wolf Willa", "role": "operator"},
    {"name": "heron_hugo", "display": "Heron Hugo", "role": "viewer"},
    {"name": "marten_mira", "display": "Marten Mira", "role": "operator"},
    {"name": "sentinel", "display": "Sentinel", "role": "viewer"},
]

_HERO_NAMES = ["operator", "falcon_finn", "heron_hugo"]

# ---------------------------------------------------------------------------
# Fake incident data
# ---------------------------------------------------------------------------

_INCIDENT_DIR = Path("/tmp/incident")


def _write_incident_data() -> None:
    """Write realistic fake log/stat files used by the demo commands."""
    _INCIDENT_DIR.mkdir(parents=True, exist_ok=True)

    # ~150 lines of realistic syslog with ERROR/FATAL lines
    syslog_lines: list[str] = []
    base_ts = "2026-04-11T14:2"
    for i in range(150):
        minute = i // 10
        second = (i * 4) % 60
        ts = f"{base_ts}{minute}:{second:02d}.{i % 1000:03d}Z"
        if i in {42, 67, 89, 91, 93, 95, 98, 100, 102, 105, 110, 115, 120, 125, 130}:
            syslog_lines.append(f"{ts} api-server ERROR: database connection timeout after 30000ms (pool exhausted)")
        elif i in {92, 103, 112, 128}:
            syslog_lines.append(f'{ts} postgres FATAL: too many connections for role "app_user"')
        elif i % 7 == 0:
            syslog_lines.append(f"{ts} api-server WARN: connection pool utilization at {75 + (i % 25)}%")
        elif i % 11 == 0:
            syslog_lines.append(f"{ts} nginx INFO: upstream response time {200 + i * 3}ms for /api/v1/orders")
        else:
            syslog_lines.append(
                f"{ts} api-server INFO: request completed status=200 path=/api/v1/health latency={12 + i % 30}ms"
            )
    (_INCIDENT_DIR / "syslog.txt").write_text("\n".join(syslog_lines) + "\n")

    # pg_stat_activity showing 47 idle-in-transaction connections
    pg_header = "  pid  |  state                |  query_start              |  query"
    pg_sep = "-------+-----------------------+---------------------------+--------------------------------------------"
    pg_rows: list[str] = [pg_header, pg_sep]
    for i in range(47):
        pid = 10200 + i
        pg_rows.append(
            f" {pid} | idle in transaction   | 2026-04-11 14:1{i % 10}:{(i * 7) % 60:02d}     "  # noqa: S608
            f"| SELECT * FROM orders WHERE customer_id = {1000 + i}"
        )
    pg_rows.extend([pg_sep, "(47 rows)"])
    (_INCIDENT_DIR / "pg_stat.txt").write_text("\n".join(pg_rows) + "\n")

    # Docker logs with ConnectionPool errors
    docker_lines: list[str] = []
    for i in range(50):
        ts = f"2026-04-11T14:2{i // 10}:{(i * 3) % 60:02d}.{i:03d}Z"
        if i % 4 == 0:
            docker_lines.append(f"{ts} [ERROR] ConnectionPool exhausted: 47/50 connections idle-in-transaction")
        elif i % 4 == 1:
            docker_lines.append(f"{ts} [WARN]  Connection acquire timeout: waited 30000ms")
        elif i % 4 == 2:
            docker_lines.append(f"{ts} [INFO]  Request completed: POST /api/v1/orders status=503 latency=30012ms")
        else:
            docker_lines.append(f"{ts} [INFO]  Health check: db=timeout api=degraded uptime=3h42m")
    (_INCIDENT_DIR / "docker_logs.txt").write_text("\n".join(docker_lines) + "\n")

    # Health check JSON responses
    (_INCIDENT_DIR / "health_degraded.json").write_text(
        '{\n  "status": "degraded",\n  "db": "timeout",\n  "api": "ok",\n  "connections": 47,\n  "pool_max": 50\n}\n'
    )
    (_INCIDENT_DIR / "health_ok.json").write_text(
        '{\n  "status": "healthy",\n  "db": "ok",\n  "api": "ok",\n  "connections": 3,\n  "pool_max": 50\n}\n'
    )

    # Docker ps output
    (_INCIDENT_DIR / "docker_ps.txt").write_text(
        textwrap.dedent("""\
        NAMES            STATUS          PORTS
        api-server       Up 3 hours      0.0.0.0:8080->8080/tcp
        postgres         Up 3 hours      0.0.0.0:5432->5432/tcp
        redis            Up 3 hours      0.0.0.0:6379->6379/tcp
        nginx            Up 3 hours      0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
        worker-1         Up 3 hours
        worker-2         Up 3 hours
        celery-beat      Up 3 hours
    """)
    )

    # Recovery docker logs (shown after fix)
    (_INCIDENT_DIR / "docker_logs_recovery.txt").write_text(
        textwrap.dedent("""\
        2026-04-11T14:31:02.100Z [INFO]  Connection pool recovered: 3/50 active connections
        2026-04-11T14:31:02.200Z [INFO]  Health check: db=ok api=ok uptime=3h44m
        2026-04-11T14:31:03.100Z [INFO]  Request completed: GET /api/v1/health status=200 latency=12ms
        2026-04-11T14:31:04.050Z [INFO]  Request completed: POST /api/v1/orders status=201 latency=45ms
        2026-04-11T14:31:05.001Z [INFO]  Connection pool stable: 5/50 active connections
    """)
    )


# ---------------------------------------------------------------------------
# Step helpers (callables for BrowserStep)
# ---------------------------------------------------------------------------


def _scroll_up(lines: int):
    """Return a BrowserStep callable that scrolls xterm up N lines via the Terminal API."""

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.evaluate(  # type: ignore[union-attr]
                f"""() => {{
                    const w = document.querySelector('.provide-hijack')?.__provideHijack;
                    if (w && w.terminal) w.terminal.scrollLines(-{lines});
                }}"""
            )

    return _do


def _scroll_to_bottom():
    """Return a BrowserStep callable that scrolls xterm to the bottom via the Terminal API."""

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.evaluate(  # type: ignore[union-attr]
                """() => {
                    const w = document.querySelector('.provide-hijack')?.__provideHijack;
                    if (w && w.terminal) w.terminal.scrollToBottom();
                }"""
            )

    return _do


def _navigate_away():
    """Return a BrowserStep callable that disconnects by navigating to about:blank."""

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.goto("about:blank")  # type: ignore[union-attr]

    return _do


def _type_from_self(text: str, wait_s: float = 1.0):
    """Return a callable that types into THIS user's own browser input field.

    Unlike ``_send_cmd`` (which always types on Tanuki Tim's page), this callable
    operates on whichever page the step interleaver passes in — proving that
    different users are actually typing.
    """
    cmd_text = text.rstrip("\r")

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.fill('[id$="-inputfield"]', cmd_text + "\\r")  # type: ignore[union-attr]
            page.click('[id$="-inputsend"]')  # type: ignore[union-attr]
        time.sleep(wait_s)

    return _do


# What each user types when they join the incident channel
_JOIN_MESSAGES: dict[str, str | None] = {
    "bear_brody": "echo '[Bear Brody] SRE on-call — joining, checking logs'",
    "crane_cara": "echo '[Crane Cara] Backend — pulling up app traces'",
    "falcon_finn": "echo '[Falcon Finn] DBA — standby, will check connection pool'",
    "lynx_liam": "echo '[Lynx Liam] Security — auditing commands'",
    "wolf_willa": "echo '[Wolf Willa] DevOps — looking at container health'",
    "heron_hugo": None,  # eng manager watches, doesn't type
    "marten_mira": "echo '[Marten Mira] QA — checking error reports'",
    "sentinel": None,  # bot doesn't type
}


# ---------------------------------------------------------------------------
# record()
# ---------------------------------------------------------------------------


def record(base_out: Path = BASE_OUT) -> dict[str, Path | None]:
    """Record the 9-person production incident response DeckMux demo.

    Produces 9 individual perspective mp4s, 3 hero mp4s (Tanuki Tim, Falcon Finn, Heron Hugo),
    and a 3-column composite video. The reel highlight comes from the composite.
    """
    feat_dir = out_dir(FEATURE, base_out)
    _write_incident_data()

    base_url, server = start_server(
        hub_class=_DeckMuxTermHub,
        sessions=[
            {
                "session_id": "provide-shell",
                "display_name": "Incident Response",
                "connector_type": "pty",
                "input_mode": "open",
                "auto_start": True,
                "connector_config": {"command": "/bin/bash"},
            }
        ],
    )
    wait_connected(base_url, "provide-shell", timeout=15.0)
    time.sleep(1.0)

    # Record terminal demo with asciinema
    cast_path = asciinema_record(__file__, feat_dir / "terminal.cast")

    # Commands are sent by typing into Tanuki Tim's browser input field (open/shared mode).
    # Using the hijack REST API puts browsers into "Hijacked (other)" observer mode
    # which shows a static snapshot instead of live terminal output.
    # The input field (id="h-provide-shell-inputfield") unescapes \\r to \r before
    # sending via WebSocket, so we append \\r to each command to send Enter.
    _tim_page: list[Any] = []  # filled by the first step callable

    def _capture_tim_page(page: object) -> None:
        """Capture Tanuki Tim's page reference for command sending."""
        _tim_page.clear()
        _tim_page.append(page)

    # --- Build perspective step lists ---
    # The "join" step for each user is navigating to the operator page.
    # Users who join later get (None, wait, None) padding steps to stay in sync.
    op_url = "/app/operator/provide-shell"

    def _wait_term(page: object) -> None:
        wait_for_terminal(page)  # type: ignore[arg-type]

    def _wait_bar(min_users: int):
        def _do(page: object) -> None:
            wait_for_presence_bar(page, min_users=min_users)  # type: ignore[arg-type]

        return _do

    # Step indices map to the script timeline.
    # Each step is (action, wait_s, screenshot).
    # Total steps: ~55 covering Acts 1-5.

    # --- ACT 1: Tanuki Tim alone, running diagnostics (steps 0-11) ---
    act1_tim: list[BrowserStep] = [
        (op_url, 1.0, None),  # 0: navigate
        (_wait_term, 1.0, None),  # 1: wait for terminal
        (_capture_tim_page, 0.5, None),  # 2: capture page ref + settle
        # Tanuki Tim runs commands — each _cmd call happens between steps via _cmd_step
    ]

    # Commands Tanuki Tim runs during Act 1 (sent via API, interleaved with step waits)
    act1_commands = [
        ("echo '[Tanuki Tim] On-call paged — investigating db connection alerts'\r", 0.8),
        ("date && hostname\r", 0.8),
        ("cat /tmp/incident/syslog.txt | tail -80\r", 1.2),
        ("grep -n 'ERROR\\|FATAL' /tmp/incident/syslog.txt\r", 1.0),
        ("ps aux --sort=-%mem | head -30\r", 1.0),
        ("cat /tmp/incident/health_degraded.json\r", 0.8),
    ]

    def _send_cmd(keys: str, wait_s: float = 0.7):
        """Return a callable that types a command into Tanuki Tim's browser input field.

        The hijack widget input field (open mode) unescapes \\\\r to \\r.
        We strip the trailing \\r from the keys arg and append \\\\r so the
        widget sends the proper carriage return to the terminal.

        Element IDs use a sequential numeric UID (e.g. ``h-1-inputfield``),
        not the session ID, so we match with ``[id$="-inputfield"]``.
        """
        cmd_text = keys.rstrip("\r")

        def _do(_page: object) -> None:
            import contextlib

            page = _tim_page[0] if _tim_page else None
            if page is None:
                return
            with contextlib.suppress(Exception):
                page.fill('[id$="-inputfield"]', cmd_text + "\\r")  # type: ignore[union-attr]
                page.click('[id$="-inputsend"]')  # type: ignore[union-attr]
            time.sleep(wait_s)

        return _do

    for keys, wait_s in act1_commands:
        act1_tim.append((_send_cmd(keys, wait_s), 0.3, None))

    # Screenshot after Act 1 commands
    act1_tim.append((None, 0.5, "act1-operator-investigating.png"))  # step ~8

    # Padding for other users during Act 1 (they haven't joined yet)
    act1_pad_len = len(act1_tim)
    act1_pad: list[BrowserStep] = [(None, 0.0, None)] * act1_pad_len

    # --- ACT 2: Team assembles (steps ~9-20) ---
    # Each user joins at a staggered point.
    # After joining, they wait for the presence bar to show enough users.
    join_order = ["bear_brody", "crane_cara", "falcon_finn", "lynx_liam", "wolf_willa", "heron_hugo", "marten_mira", "sentinel"]
    join_steps_per_user: dict[str, list[BrowserStep]] = {}
    for idx, uname in enumerate(join_order):
        pre_pad: list[BrowserStep] = [(None, 0.0, None)] * idx  # stagger
        join: list[BrowserStep] = [
            (op_url, 0.8, None),
            (_wait_term, 0.5, None),
        ]
        # Each user types their own message from their own browser after joining
        msg = _JOIN_MESSAGES.get(uname)
        if msg:
            join.append((_type_from_self(msg), 0.3, None))
        else:
            join.append((None, 0.3, None))
        join_steps_per_user[uname] = pre_pad + join

    # Tanuki Tim during Act 2: waits, then announces
    act2_tim: list[BrowserStep] = [
        (None, 1.0, None),  # wait for first joins
    ]
    # Pad to align with longest join sequence
    max_join_len = max(len(s) for s in join_steps_per_user.values())
    while len(act2_tim) < max_join_len:
        act2_tim.append((None, 0.8, None))
    # Wait for full bar then announce
    act2_tim.append((_wait_bar(9), 1.0, "act2-full-team.png"))
    act2_tim.append((_send_cmd("echo '--- team assembled, investigating db connection timeouts ---'\r"), 1.0, None))
    act2_tim.append((None, 3.0, None))  # 15s settle (compressed across steps)

    # Pad other users' Act 2 steps to same length as Tanuki Tim's
    for uname in join_order:
        while len(join_steps_per_user[uname]) < len(act2_tim):
            join_steps_per_user[uname].append((None, 0.3, None))

    # --- ACT 3: Investigation — scrolling spreads edge bars (steps ~21-35) ---
    act3_commands = [
        ("cat /tmp/incident/docker_ps.txt\r", 0.8),
        ("cat /tmp/incident/docker_logs.txt | tail -40\r", 1.0),
        ("cat /tmp/incident/pg_stat.txt\r", 1.0),
        ("echo 'root cause: connection pool exhaustion -- 47 idle-in-transaction sessions'\r", 0.8),
    ]

    # Tanuki Tim sends commands
    act3_tim: list[BrowserStep] = []
    for keys, wait_s in act3_commands:
        act3_tim.append((_send_cmd(keys, wait_s), 1.5, None))
    act3_tim.append((None, 2.0, "act3-investigation.png"))

    act3_len = len(act3_tim)

    # Other users scroll at specific steps during Act 3
    def _act3_for(scroll_at_step: int | None, scroll_lines: int) -> list[BrowserStep]:
        steps: list[BrowserStep] = []
        for i in range(act3_len):
            if i == scroll_at_step:
                steps.append((_scroll_up(scroll_lines), 1.0, None))
            else:
                steps.append((None, 0.3, None))
        return steps

    act3_users: dict[str, list[BrowserStep]] = {
        "bear_brody": _act3_for(0, 60),  # scrolls to syslog errors
        "crane_cara": _act3_for(1, 120),  # scrolls to raw syslog
        "falcon_finn": _act3_for(None, 0),  # stays near bottom (DBA reading pg_stat)  # type: ignore[arg-type]
        "lynx_liam": _act3_for(2, 30),  # scrolls to audit curl output
        "wolf_willa": _act3_for(1, 90),  # scrolls to ps aux
        "heron_hugo": _act3_for(None, 0),  # viewer, doesn't scroll  # type: ignore[arg-type]
        "marten_mira": _act3_for(2, 50),  # QA checking docker logs
        "sentinel": _act3_for(None, 0),  # bot, doesn't scroll  # type: ignore[arg-type]
    }

    # --- ACT 4: Handoff & fix ---
    # Falcon Finn types from HIS browser (proving multi-user input).
    # Bear Brody types from HIS browser when verifying.
    # Tanuki Tim only types the handoff announcements.

    # Falcon Finn's commands (typed from Falcon Finn's page via _type_from_self)
    brandon_fix_cmds = [
        "echo '[Falcon Finn] Taking over — checking idle connections'",
        "echo '[Falcon Finn] psql> SELECT count(*) FROM pg_stat_activity WHERE state = idle_in_transaction'",
        "echo '[Falcon Finn]   count: 47'",
        "echo '[Falcon Finn] psql> SELECT pg_terminate_backend(pid) ... WHERE query_start < now() - 5min'",
        "echo '[Falcon Finn]   terminated: 47 connections'",
        "echo '[Falcon Finn] psql> SELECT count(*) ... idle_in_transaction'",
        "echo '[Falcon Finn]   count: 0 — pool cleared'",
    ]

    # Bear Brody's verification commands (typed from Bear Brody's page)
    kal_verify_cmds = [
        "echo '[Bear Brody] Verifying fix — checking health endpoint'",
        "cat /tmp/incident/health_ok.json",
        "cat /tmp/incident/docker_logs_recovery.txt",
    ]

    # Tanuki Tim's Act 4 steps: handoff announcements + wait slots aligned with Falcon Finn/Bear Brody
    act4_tim: list[BrowserStep] = [
        (_send_cmd("echo '[Tanuki Tim] Handing terminal to Falcon Finn (DBA)'\r"), 1.5, None),  # 0
    ]
    # Wait while Falcon Finn types (one step per Falcon Finn command)
    act4_tim.extend((None, 1.5, None) for _ in brandon_fix_cmds)
    act4_tim.append((None, 1.0, "act4-falcon_finn-fix.png"))  # screenshot
    act4_tim.append((_send_cmd("echo '[Tanuki Tim] Falcon Finn done — Bear Brody, can you verify?'\r"), 1.5, None))
    # Wait while Bear Brody verifies
    act4_tim.extend((None, 1.5, None) for _ in kal_verify_cmds)
    act4_tim.append((None, 1.0, "act4-bear_brody-verified.png"))  # screenshot
    act4_tim.append((_send_cmd("echo '[Tanuki Tim] Fix confirmed — taking back control'\r"), 1.0, None))

    act4_len = len(act4_tim)

    # Falcon Finn's Act 4: types his fix commands from his own browser
    act4_brandon: list[BrowserStep] = [(None, 1.5, None)]  # step 0: wait for Tanuki Tim's handoff msg
    act4_brandon.extend((_type_from_self(cmd, wait_s=1.0), 0.3, None) for cmd in brandon_fix_cmds)
    while len(act4_brandon) < act4_len:
        act4_brandon.append((None, 0.3, None))

    # Bear Brody's Act 4: scrolls to bottom then types verification from his own browser
    kal_verify_start = len(brandon_fix_cmds) + 3  # after Falcon Finn screenshot + Tanuki Tim's handoff
    act4_kal: list[BrowserStep] = []
    for i in range(act4_len):
        if i == kal_verify_start - 1:
            act4_kal.append((_scroll_to_bottom(), 0.5, None))
        elif i >= kal_verify_start and (i - kal_verify_start) < len(kal_verify_cmds):
            act4_kal.append((_type_from_self(kal_verify_cmds[i - kal_verify_start], wait_s=1.0), 0.3, None))
        else:
            act4_kal.append((None, 0.3, None))

    act4_pad: list[BrowserStep] = [(None, 0.3, None)] * act4_len

    # --- ACT 5: Resolution & departure (steps ~51-58) ---
    act5_tim: list[BrowserStep] = [
        (_send_cmd("echo '=== INCIDENT RESOLVED ==='\r"), 0.8, None),  # 0
        (_send_cmd("echo 'fix: terminated 47 stuck connections, pool recovered'\r"), 1.0, None),  # 1
        (None, 3.0, None),  # 2: pause before departures
        (None, 3.0, "act5-lynx_liam-left.png"),  # 3: Lynx Liam disconnects
        (None, 3.0, None),  # 4: Wolf Willa disconnects
        (None, 3.0, None),  # 5: Marten Mira disconnects
        (None, 3.0, None),  # 6: Crane Cara disconnects
        (None, 3.0, "act5-resolved.png"),  # 7: final — 5 remain
    ]

    act5_len = len(act5_tim)

    # Users who disconnect
    def _act5_depart(depart_step: int) -> list[BrowserStep]:
        steps: list[BrowserStep] = []
        for i in range(act5_len):
            if i == depart_step:
                steps.append((_navigate_away(), 0.3, None))
            else:
                steps.append((None, 0.3, None))
        return steps

    act5_users: dict[str, list[BrowserStep]] = {
        "bear_brody": [(None, 0.3, None)] * act5_len,  # stays
        "crane_cara": _act5_depart(6),  # departs late
        "falcon_finn": [(None, 0.3, None)] * act5_len,  # stays
        "lynx_liam": _act5_depart(3),  # departs first
        "wolf_willa": _act5_depart(4),  # departs second
        "heron_hugo": [(None, 0.3, None)] * act5_len,  # stays (viewer)
        "marten_mira": _act5_depart(5),  # departs third
        "sentinel": [(None, 0.3, None)] * act5_len,  # stays (bot)
    }

    # --- Assemble full step sequences per perspective ---
    all_steps: dict[str, list[BrowserStep]] = {}

    # Tanuki Tim's full sequence
    all_steps["operator"] = act1_tim + act2_tim + act3_tim + act4_tim + act5_tim

    # Other users: padding during Act 1, join during Act 2, act during 3-5
    for uname in join_order:
        steps: list[BrowserStep] = []
        steps.extend(act1_pad)  # Act 1: not yet joined
        steps.extend(join_steps_per_user[uname])  # Act 2: join + settle
        steps.extend(act3_users.get(uname, [(None, 0.3, None)] * act3_len))  # Act 3
        if uname == "bear_brody":
            steps.extend(act4_kal)  # Act 4: Bear Brody verifies from his browser
        elif uname == "falcon_finn":
            steps.extend(act4_brandon)  # Act 4: Falcon Finn types fix from his browser
        else:
            steps.extend(act4_pad)  # Act 4: watching
        steps.extend(act5_users.get(uname, [(None, 0.3, None)] * act5_len))  # Act 5
        all_steps[uname] = steps

    # Build context_options with X-Display-Name header per perspective
    ctx_opts: dict[str, dict[str, Any]] = {}
    for member in _CAST:
        ctx_opts[member["name"]] = {
            "extra_http_headers": {
                "X-Display-Name": member["display"],
                "X-Principal": f"user-{member['name']}",  # unique principal per context
            },
        }

    print(
        f"  Recording {len(all_steps)} perspectives ({sum(len(s) for s in all_steps.values())} total steps)...",
        flush=True,
    )
    vids = record_simultaneous_perspectives(all_steps, base_url, feat_dir, context_options=ctx_opts)

    stop_server(server)

    # --- Post-production: 3x3 grid composite of all 9 perspectives ---
    all_clips = [vids.get(m["name"]) for m in _CAST]
    composite = hstack_clips(all_clips, feat_dir / "composite.mp4")
    highlight = trim_clip(composite, HIGHLIGHT_START_S, HIGHLIGHT_DURATION_S)

    result: dict[str, Any] = {"cast": cast_path, "composite": composite, "highlight": highlight}
    for member in _CAST:
        result[f"{member['name']}_mp4"] = vids.get(member["name"])
    return result


async def run_terminal_demo() -> None:
    """Run the DeckMux presence demo (terminal-only, for asciinema)."""
    import httpx

    from scripts.demos import banner, info, kv, ok

    base_url, server = start_server(
        hub_class=_DeckMuxTermHub,
        sessions=[
            {
                "session_id": "provide-shell",
                "display_name": "Provide Shell",
                "connector_type": "pty",
                "input_mode": "open",
                "auto_start": True,
                "connector_config": {"command": "/bin/bash"},
            }
        ],
    )
    time.sleep(1.5)

    banner(DESCRIPTION)

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, headers=dev_bearer_headers()) as http:
        info("Fetching session info...")
        r = await http.get("/api/sessions/provide-shell")
        r.raise_for_status()
        session = r.json()
        kv("session_id", session.get("session_id"))
        kv("connected", session.get("connected"))

        info("Fetching terminal snapshot...")
        r = await http.get("/api/sessions/provide-shell/snapshot")
        snapshot = r.json() or {}
        kv("cols", snapshot.get("cols"))
        kv("rows", snapshot.get("rows"))

    info("(Presence cursors are live via WebSocket -- see browser recording)")
    ok("DeckMux presence enabled -- multiple cursors share the session")
    stop_server(server)


if __name__ == "__main__":
    if "--run-demo" in sys.argv:
        asyncio.run(run_terminal_demo())
    else:
        result = record()
        print(f"\nDeckMux demo: {result}")
