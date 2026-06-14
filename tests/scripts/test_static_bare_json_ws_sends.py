#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_bare_json_ws_sends.py"
_spec = importlib.util.spec_from_file_location("check_bare_json_ws_sends", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
checker = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = checker
_spec.loader.exec_module(checker)


def _find(tmp_path: Path, rel: str, text: str) -> list[str]:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return [violation.message for violation in checker.find_violations([path])]


def test_flags_python_direct_json_send(tmp_path: Path) -> None:
    messages = _find(
        tmp_path,
        "src/provide/uterm/control/ws_path.py",
        "import json\nasync def f(ws):\n    await ws.send(json.dumps({'type': 'input'}))\n",
    )
    assert messages == ["bare JSON WebSocket send; use a framed control/tunnel codec"]


def test_flags_python_assigned_payload_send(tmp_path: Path) -> None:
    messages = _find(
        tmp_path,
        "src/provide/uterm/terminal/session.py",
        "import json\nasync def f(control_ws):\n    payload = json.dumps({'type': 'input'})\n    await control_ws.send(payload)\n",
    )
    assert messages == ["bare JSON WebSocket send via JSON-serialized variable 'payload'"]


def test_flags_python_helper_encoded_send(tmp_path: Path) -> None:
    messages = _find(
        tmp_path,
        "src/provide/uterm/control/session.py",
        "import json\n"
        "def encode_control(payload):\n"
        "    return json.dumps(payload)\n"
        "async def f(ws):\n"
        "    await ws.send(encode_control({'type': 'input'}))\n",
    )
    assert messages == ["bare JSON WebSocket send via JSON helper 'encode_control'"]


def test_flags_typescript_json_stringify_send(tmp_path: Path) -> None:
    messages = _find(
        tmp_path,
        "packages/provide-uterm-frontend/src/terminal-control.ts",
        "export function send(ws: WebSocket, msg: unknown) { ws.send(JSON.stringify(msg)); }\n",
    )
    assert messages == ["bare JSON WebSocket send; use a framed control/tunnel codec"]


def test_ignores_http_fetch_body(tmp_path: Path) -> None:
    assert (
        _find(
            tmp_path,
            "packages/provide-uterm-frontend/src/terminal-control.ts",
            "fetch('/api', { method: 'POST', body: JSON.stringify({ok: true}) });\n",
        )
        == []
    )


def test_ignores_binary_channel_http_tunnel_frame(tmp_path: Path) -> None:
    assert (
        _find(
            tmp_path,
            "packages/provide-uterm-server/src/provide/uterm/cli/inspect.py",
            "import json\nasync def f(ws):\n"
            "    await ws.send(encode_frame(CHANNEL_HTTP, json.dumps({'type': 'http_req'}).encode()))\n",
        )
        == []
    )
