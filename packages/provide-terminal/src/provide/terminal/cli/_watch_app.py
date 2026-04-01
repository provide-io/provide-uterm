#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Textual TUI app for ``uterm watch``."""

from __future__ import annotations

import json
from base64 import b64decode
from dataclasses import dataclass, field
from typing import Any, ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static

_DLE = "\x10"
_STX = "\x02"


@dataclass
class Exchange:
    """One HTTP request/response pair."""

    req_id: str
    method: str = ""
    url: str = ""
    req_headers: dict[str, str] = field(default_factory=dict)
    req_body_b64: str | None = None
    req_body_size: int = 0
    req_body_truncated: bool = False
    req_body_binary: bool = False
    status: int | None = None
    status_text: str = ""
    duration_ms: float | None = None
    res_headers: dict[str, str] = field(default_factory=dict)
    res_body_b64: str | None = None
    res_body_size: int = 0
    res_body_truncated: bool = False
    res_body_binary: bool = False


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def status_style(status: int | None) -> str:
    if status is None:
        return "dim"
    if status >= 500:
        return "bold red"
    if status >= 400:
        return "bold yellow"
    if status >= 300:
        return "yellow"
    return "bold green"


def parse_http_frames(raw: str) -> list[dict[str, Any]]:
    """Extract HTTP channel frames from a control-channel-encoded WS message."""
    frames: list[dict[str, Any]] = []
    pos = 0
    while pos < len(raw):
        idx = raw.find(_DLE, pos)
        if idx == -1:
            break
        if idx + 1 < len(raw) and raw[idx + 1] == _STX:
            header = raw[idx + 2 : idx + 10]
            if len(header) == 8 and idx + 10 < len(raw) and raw[idx + 10] == ":":
                length = int(header, 16)
                payload = raw[idx + 11 : idx + 11 + length]
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict) and obj.get("_channel") == "http":
                        frames.append(obj)
                except (json.JSONDecodeError, ValueError):
                    pass
                pos = idx + 11 + length
                continue
        pos = idx + 1
    return frames


def _decode_body(b64: str | None, truncated: bool, binary: bool, size: int) -> str:
    if b64:
        try:
            return b64decode(b64).decode("utf-8", errors="replace")
        except Exception:
            return "(decode error)"
    if truncated:
        return f"(truncated, {human_size(size)})"
    if binary:
        return f"(binary, {human_size(size)})"
    return ""


class DetailScreen(ModalScreen[None]):
    """Modal overlay showing request/response detail."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [Binding("escape", "dismiss", "Close")]

    def __init__(self, exchange: Exchange) -> None:
        super().__init__()
        self.exchange = exchange

    def compose(self) -> ComposeResult:
        ex = self.exchange
        lines = _detail_lines(ex)
        yield Static("\n".join(lines), id="detail-content")

    DEFAULT_CSS = """
    DetailScreen { align: center middle; }
    #detail-content {
        width: 80%;
        max-height: 80%;
        overflow-y: auto;
        background: $surface;
        padding: 1 2;
        border: thick $primary;
    }
    """


def _detail_lines(ex: Exchange) -> list[str]:
    lines = [f"[bold]{ex.method} {ex.url}[/bold]"]
    if ex.status is not None:
        lines.append(f"[{status_style(ex.status)}]{ex.status} {ex.status_text}[/] — {ex.duration_ms:.0f}ms")
    lines.append("")
    lines.append("[bold]Request Headers[/bold]")
    for k, v in ex.req_headers.items():
        lines.append(f"  [cyan]{k}:[/cyan] {v}")
    req_body = _decode_body(ex.req_body_b64, ex.req_body_truncated, ex.req_body_binary, ex.req_body_size)
    if req_body:
        lines.append("\n[bold]Request Body[/bold]")
        lines.append(req_body)
    if ex.status is not None:
        lines.append("\n[bold]Response Headers[/bold]")
        for k, v in ex.res_headers.items():
            lines.append(f"  [cyan]{k}:[/cyan] {v}")
        res_body = _decode_body(ex.res_body_b64, ex.res_body_truncated, ex.res_body_binary, ex.res_body_size)
        if res_body:
            lines.append("\n[bold]Response Body[/bold]")
            lines.append(res_body)
    return lines


class WatchApp(App[None]):
    """Textual TUI for watching HTTP tunnel traffic."""

    TITLE = "uterm watch"
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("q", "quit", "Quit"),
        Binding("l", "cycle_layout", "Layout"),
        Binding("f", "cycle_method", "Method"),
    ]

    CSS = """
    #request-table { height: 1fr; }
    #detail-pane { height: 1fr; width: 1fr; overflow-y: auto; padding: 0 1; }
    #status-bar { height: 1; background: $primary-background; padding: 0 1; }
    """

    def __init__(self, ws_url: str, tunnel_id: str, initial_layout: str = "horizontal") -> None:
        super().__init__()
        self._ws_url = ws_url
        self._tunnel_id = tunnel_id
        self._layout_mode = initial_layout
        self._exchanges: list[Exchange] = []
        self._method_filter: str = ""
        self._connected = False
        self._request_count = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="request-table")
        yield Static("", id="detail-pane")
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#request-table", DataTable)
        table.add_columns("Method", "URL", "Status", "Duration", "Size")
        table.cursor_type = "row"
        self._update_status()
        self._connect_ws()

    @work(thread=True)
    def _connect_ws(self) -> None:  # pragma: no cover — requires real WebSocket server
        import websockets.sync.client as wsc

        try:
            ws = wsc.connect(self._ws_url)
            self._connected = True
            self.call_from_thread(self._update_status)
            while True:
                try:
                    msg = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                if isinstance(msg, str):
                    frames = parse_http_frames(msg)
                    for frame in frames:
                        self.call_from_thread(self._handle_frame, frame)
        except Exception:
            self._connected = False
            self.call_from_thread(self._update_status)

    def _handle_frame(self, frame: dict[str, Any]) -> None:
        ftype = frame.get("type")
        if ftype == "http_req":
            ex = Exchange(
                req_id=str(frame.get("id", "")),
                method=str(frame.get("method", "")),
                url=str(frame.get("url", "")),
                req_headers=frame.get("headers") or {},
                req_body_b64=frame.get("body_b64"),
                req_body_size=int(frame.get("body_size", 0)),
                req_body_truncated=bool(frame.get("body_truncated")),
                req_body_binary=bool(frame.get("body_binary")),
            )
            self._exchanges.append(ex)
            self._request_count = len(self._exchanges)
            self._add_table_row(ex)
            self._update_status()
        elif ftype == "http_res":
            rid = str(frame.get("id", ""))
            for ex in reversed(self._exchanges):
                if ex.req_id == rid:
                    ex.status = int(frame.get("status", 0))
                    ex.status_text = str(frame.get("status_text", ""))
                    ex.duration_ms = float(frame.get("duration_ms", 0))
                    ex.res_headers = frame.get("headers") or {}
                    ex.res_body_b64 = frame.get("body_b64")
                    ex.res_body_size = int(frame.get("body_size", 0))
                    ex.res_body_truncated = bool(frame.get("body_truncated"))
                    ex.res_body_binary = bool(frame.get("body_binary"))
                    self._update_table_row(ex)
                    break

    def _add_table_row(self, ex: Exchange) -> None:
        if self._method_filter and ex.method != self._method_filter:
            return
        table = self.query_one("#request-table", DataTable)
        table.add_row(ex.method, ex.url, "...", "-", "-", key=ex.req_id)
        table.scroll_end()

    def _update_table_row(self, ex: Exchange) -> None:
        table = self.query_one("#request-table", DataTable)
        for i, rk in enumerate(table.rows):
            if rk.value == ex.req_id:
                table.update_cell_at(Coordinate(i, 2), str(ex.status or "..."))
                table.update_cell_at(Coordinate(i, 3), f"{ex.duration_ms:.0f}ms" if ex.duration_ms else "-")
                table.update_cell_at(Coordinate(i, 4), human_size(ex.res_body_size))
                break

    def _update_status(self) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
        except Exception:
            return  # Widget not mounted yet (e.g., during startup/teardown)
        conn = "Connected" if self._connected else "Disconnected"
        bar.update(f" {self._tunnel_id}  {conn}  {self._request_count} requests  [l] Layout  [f] Method  [q] Quit")

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        rid = str(event.row_key.value)
        ex = next((e for e in self._exchanges if e.req_id == rid), None)
        if ex is None:
            return
        if self._layout_mode == "modal":
            self.push_screen(DetailScreen(ex))
        else:
            pane = self.query_one("#detail-pane", Static)
            pane.update("\n".join(_detail_lines(ex)))

    _METHODS: ClassVar[list[str]] = ["", "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

    def action_cycle_layout(self) -> None:
        modes = ["horizontal", "vertical", "modal"]
        idx = modes.index(self._layout_mode)
        self._layout_mode = modes[(idx + 1) % len(modes)]
        self._update_status()

    def action_cycle_method(self) -> None:
        idx = self._METHODS.index(self._method_filter) if self._method_filter in self._METHODS else 0
        self._method_filter = self._METHODS[(idx + 1) % len(self._METHODS)]
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        table = self.query_one("#request-table", DataTable)
        table.clear()
        for ex in self._exchanges:
            if self._method_filter and ex.method != self._method_filter:
                continue
            status = str(ex.status) if ex.status else "..."
            dur = f"{ex.duration_ms:.0f}ms" if ex.duration_ms else "-"
            size = human_size(ex.res_body_size) if ex.status else "-"
            table.add_row(ex.method, ex.url, status, dur, size, key=ex.req_id)
