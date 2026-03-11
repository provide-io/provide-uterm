#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""V8 browser coverage gates for shipped frontend assets."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

_DEFAULT_REPORT_PATH = Path(".tmp-frontend-coverage/frontend-v8-coverage.json")


def _coverage_report_path() -> Path:
    configured = os.environ.get("PROVIDE_TERMINAL_FRONTEND_COVERAGE_PATH")
    return Path(configured) if configured else _DEFAULT_REPORT_PATH


def _write_coverage_result(asset: str, covered: int, total: int) -> None:
    path = _coverage_report_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any]
    report = json.loads(path.read_text()) if path.exists() else {"assets": {}}
    ratio = covered / total if total else 0.0
    assets = report.setdefault("assets", {})
    assets[asset] = {
        "covered_bytes": covered,
        "total_bytes": total,
        "ratio": ratio,
    }
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def _start_precise_coverage(page: Page) -> Any:
    session = page.context.new_cdp_session(page)
    session.send("Profiler.enable")
    session.send("Debugger.enable")
    session.send("Profiler.startPreciseCoverage", {"callCount": False, "detailed": True})
    return session


def _stop_precise_coverage(session: Any) -> list[dict[str, Any]]:
    result = session.send("Profiler.takePreciseCoverage")
    session.send("Profiler.stopPreciseCoverage")
    return result["result"]


def _covered_bytes_for_script(session: Any, scripts: list[dict[str, Any]], suffix: str) -> tuple[int, int]:
    script = next(item for item in scripts if str(item.get("url", "")).endswith(suffix))
    source = session.send("Debugger.getScriptSource", {"scriptId": script["scriptId"]})["scriptSource"]
    total = len(source)
    covered_offsets: set[int] = set()
    for fn in script["functions"]:
        for entry in fn["ranges"]:
            if entry["count"] <= 0:
                continue
            start = max(0, int(entry["startOffset"]))
            end = min(total, int(entry["endOffset"]))
            covered_offsets.update(range(start, end))
    return len(covered_offsets), total


class TestFrontendV8Coverage:
    def test_terminal_asset_meets_v8_coverage_threshold(
        self, page: Page, terminal_proxy_server: tuple[str, list[bytes]]
    ) -> None:
        base_url, _received = terminal_proxy_server
        session = _start_precise_coverage(page)
        page.goto(f"{base_url}/terminal/terminal.html", wait_until="domcontentloaded")

        expect(page.locator(".terminal-div")).to_be_visible(timeout=5000)
        expect(page.locator(".loading")).to_be_hidden(timeout=5000)
        page.wait_for_function("Boolean(window.demoTerminal)")
        page.evaluate("window.demoTerminal.handleTerminalInput('cov\\r')")
        page.get_by_role("button", name="Settings").click()
        page.get_by_role("button", name="Glass").click()
        page.locator("[id^='setFontSize-']").fill("15")
        page.locator("[id^='setFontSize-']").dispatch_event("input")
        page.locator("[id^='fxGlow-']").check()
        page.evaluate("window.demoTerminal.ws.close()")
        page.wait_for_timeout(1200)

        scripts = _stop_precise_coverage(session)
        covered, total = _covered_bytes_for_script(session, scripts, "/terminal.js")
        session.detach()
        _write_coverage_result("terminal.js", covered, total)
        ratio = covered / total if total else 0.0
        assert ratio >= 1.0, f"terminal.js V8 coverage too low: {ratio:.3%}"

    def test_hijack_asset_meets_v8_coverage_threshold(self, page: Page, example_server: str) -> None:
        with httpx.Client(base_url=example_server, timeout=5.0) as http:
            reset = http.post("/demo/session/demo-session/reset")
            assert reset.status_code == 200
            mode = http.post("/demo/session/demo-session/mode", json={"input_mode": "hijack"})
            assert mode.status_code == 200
        session = _start_precise_coverage(page)
        page.goto(f"{example_server}/hijack/hijack.html?worker=demo-session", wait_until="domcontentloaded")

        expect(page.locator("#demo-session-status")).to_contain_text("demo-session", timeout=5000)
        expect(page.get_by_role("button", name="Hijack")).to_be_enabled(timeout=5000)
        page.get_by_role("button", name="Hijack").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Hijacked (you)", timeout=5000)
        page.locator("button[title='Mobile key toolbar']").click()
        expect(page.locator(".mobile-keys")).to_be_visible(timeout=5000)
        page.locator("[id$='-inputfield']").fill("/status")
        page.get_by_role("button", name="Send").click()
        page.get_by_role("button", name="Analyze").click()
        expect(page.locator("[id$='-analysistext']")).not_to_be_empty(timeout=5000)
        page.get_by_role("button", name="⟳ Resync").click()
        page.get_by_role("button", name="Release").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (watching)", timeout=5000)
        page.select_option("#demo-mode", "open")
        page.locator("#demo-apply").click()
        expect(page.locator("[id$='-statustext']")).to_have_text("Connected (shared)", timeout=5000)

        scripts = _stop_precise_coverage(session)
        covered, total = _covered_bytes_for_script(session, scripts, "/hijack.js")
        session.detach()
        _write_coverage_result("hijack.js", covered, total)
        ratio = covered / total if total else 0.0
        assert ratio >= 1.0, f"hijack.js V8 coverage too low: {ratio:.3%}"
