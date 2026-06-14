#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Scenario data and BrowserStep helpers for the DeckMux recording demo."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Fake incident data and scenario helpers
# ---------------------------------------------------------------------------

INCIDENT_DIR = Path("/tmp/incident")


def write_incident_data() -> None:
    """Write realistic fake log/stat files used by the demo commands."""
    INCIDENT_DIR.mkdir(parents=True, exist_ok=True)

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
    (INCIDENT_DIR / "syslog.txt").write_text("\n".join(syslog_lines) + "\n")

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
    (INCIDENT_DIR / "pg_stat.txt").write_text("\n".join(pg_rows) + "\n")

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
    (INCIDENT_DIR / "docker_logs.txt").write_text("\n".join(docker_lines) + "\n")

    # Health check JSON responses
    (INCIDENT_DIR / "health_degraded.json").write_text(
        '{\n  "status": "degraded",\n  "db": "timeout",\n  "api": "ok",\n  "connections": 47,\n  "pool_max": 50\n}\n'
    )
    (INCIDENT_DIR / "health_ok.json").write_text(
        '{\n  "status": "healthy",\n  "db": "ok",\n  "api": "ok",\n  "connections": 3,\n  "pool_max": 50\n}\n'
    )

    # Docker ps output
    (INCIDENT_DIR / "docker_ps.txt").write_text(
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
    (INCIDENT_DIR / "docker_logs_recovery.txt").write_text(
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


def scroll_up(lines: int):
    """Return a BrowserStep callable that scrolls xterm up N lines via the Terminal API."""

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.evaluate(  # type: ignore[union-attr]
                f"""() => {{
                    const t = document.querySelector('uterm-session')?.terminal;
                    if (t) t.scrollLines(-{lines});
                }}"""
            )

    return _do


def scroll_to_bottom():
    """Return a BrowserStep callable that scrolls xterm to the bottom via the Terminal API."""

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.evaluate(  # type: ignore[union-attr]
                """() => {
                    const t = document.querySelector('uterm-session')?.terminal;
                    if (t) t.scrollToBottom();
                }"""
            )

    return _do


def navigate_away():
    """Return a BrowserStep callable that disconnects by navigating to about:blank."""

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.goto("about:blank")  # type: ignore[union-attr]

    return _do


def type_from_self(text: str, wait_s: float = 1.0):
    """Return a callable that types into THIS user's own browser input field.

    Unlike ``_send_cmd`` (which always types on Tanuki Tim's page), this callable
    operates on whichever page the step interleaver passes in — proving that
    different users are actually typing.
    """
    cmd_text = text.rstrip("\r")

    def _do(page: object) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            page.fill("#inputfield", cmd_text + "\\r")  # type: ignore[union-attr]
            page.click("#inputsend")  # type: ignore[union-attr]
        time.sleep(wait_s)

    return _do


# What each user types when they join the incident channel
JOIN_MESSAGES: dict[str, str | None] = {
    "bear_brody": "echo '[Bear Brody] SRE on-call — joining, checking logs'",
    "crane_cara": "echo '[Crane Cara] Backend — pulling up app traces'",
    "falcon_finn": "echo '[Falcon Finn] DBA — standby, will check connection pool'",
    "lynx_liam": "echo '[Lynx Liam] Security — auditing commands'",
    "wolf_willa": "echo '[Wolf Willa] DevOps — looking at container health'",
    "heron_hugo": None,  # eng manager watches, doesn't type
    "marten_mira": "echo '[Marten Mira] QA — checking error reports'",
    "sentinel": None,  # bot doesn't type
}
