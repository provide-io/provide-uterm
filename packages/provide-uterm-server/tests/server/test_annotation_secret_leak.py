#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""A recording never carries a credential through an annotation description.

The recording redacts what it records as ``send``/``read`` data, but an
annotation is written with ``SessionLogger.log_event``, which applies no
redactor. So with ``redact_sensitive`` ON, ``curl -H 'Authorization: Bearer
<token>' https://...`` used to land in the recording twice: once redacted (the
send event) and once in clear, inside the ``conn.curl`` annotation's
description. The detector now masks credential-shaped text before it embeds a
match; these tests drive the real runtime and the real recording file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from provide.uterm.annotation import PatternDetector
from provide.uterm.server.bridge.hub.redaction import StreamRedactor
from provide.uterm.server.bridge.hub.redaction_defaults import default_rules
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime
from provide.uterm.session_logger import SessionLogger

if TYPE_CHECKING:
    from pathlib import Path

BEARER = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"  # pragma: allowlist secret
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret


async def _record_send(tmp_path: Path, data: str) -> list[dict[str, Any]]:
    runtime = HostedSessionRuntime(
        SessionDefinition(session_id="leak-1", display_name="Leak", connector_type="shell", auto_start=False),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
        detector=PatternDetector(),
    )
    path = tmp_path / "rec.jsonl"
    logger = SessionLogger(path, redactor=StreamRedactor(default_rules()).redact)
    await logger.start("leak-1")
    runtime._logger = logger
    await runtime._log_send(data)
    await logger.stop()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_bearer_token_on_a_curl_line_never_reaches_the_recording(tmp_path: Path) -> None:
    entries = await _record_send(tmp_path, f"curl -H 'Authorization: Bearer {BEARER}' https://api.example.com\r")

    raw = json.dumps(entries)
    assert BEARER not in raw
    assert BEARER[:8] not in raw
    descriptions = [e["data"]["description"] for e in entries if e["event"] == "annotation"]
    assert descriptions == [
        "Bearer token detected in send",
        "curl HTTP request detected: curl -H 'Authorization: [SECRET]' https://",
    ]


async def test_an_80_char_cut_inside_a_key_leaves_no_fragment(tmp_path: Path) -> None:
    # The cut used to stop 10 characters into the key. "AKIAIOSFOD" is too short
    # for the default 20-character AWS rule, so even a redactor applied to the
    # annotation would have let it through.
    pad = "x" * (80 - len("curl -d ") - 10 - 1) + "="
    entries = await _record_send(tmp_path, f"curl -d {pad}{AWS_KEY} https://x.example\r")

    assert StreamRedactor(default_rules()).redact(AWS_KEY[:10]) == AWS_KEY[:10]
    assert "AKIA" not in json.dumps(entries)
