#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Recorded annotations honour ``recording.redact_sensitive``.

Detector annotations quote the matched text in their description (the curl
rule's ``\\bcurl\\b.*https?://`` captures everything between ``curl`` and the
URL), and operator annotations carry a free-form description. Both reach the
recording through ``SessionLogger.log_event``, which used to bypass the
redactor that already scrubbed the screen and keystrokes beside them.

``password=S3cretValue`` is chosen because the default recording rules
(``redaction_defaults._GENERIC_PASSWORD``) redact it *and* the curl detector
rule embeds it in its description.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from provide.uterm.annotation import PatternDetector
from provide.uterm.recording import InMemoryRecordingStore
from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.models import RecordingConfig, SessionDefinition
from provide.uterm.server.runtime import HostedSessionRuntime

_SECRET = "S3cretValue"  # pragma: allowlist secret
_SCREEN = f"$ curl --data password={_SECRET} https://example.com/login\n"


async def _recorded_annotations(*, redact_sensitive: bool, send: bool = False) -> list[dict[str, Any]]:
    store = InMemoryRecordingStore()
    rt = HostedSessionRuntime(
        SessionDefinition(session_id="ann-red", display_name="x", connector_type="shell", auto_start=False),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(enabled_by_default=True, redact_sensitive=redact_sensitive),
        recording_store=store,
        detector=PatternDetector(),
    )
    await rt._start_recording()
    if send:
        await rt._log_send(_SCREEN)
    else:
        await rt._log_snapshot({"screen": _SCREEN})
    await rt._stop_recording()
    entries = await store.get_entries("ann-red", limit=500)
    return [e["data"] for e in entries if e["event"] == "annotation"]


def _curl_description(annotations: list[dict[str, Any]]) -> str:
    return next(a["description"] for a in annotations if a["description"].startswith("curl HTTP request"))


async def test_detector_annotation_is_redacted_when_redact_sensitive_on() -> None:
    annotations = await _recorded_annotations(redact_sensitive=True)
    description = _curl_description(annotations)
    assert _SECRET not in description
    assert "[PASSWORD_REDACTED]" in description
    # Nothing in any recorded annotation carries the secret.
    assert all(_SECRET not in str(a) for a in annotations)
    # Non-string fields survive unchanged.
    assert annotations[0]["span"] == {"from_seq": 1, "to_seq": 1}


async def test_send_side_detector_annotation_is_redacted() -> None:
    annotations = await _recorded_annotations(redact_sensitive=True, send=True)
    assert annotations
    assert all(_SECRET not in str(a) for a in annotations)


async def test_detector_annotation_is_verbatim_when_redact_sensitive_off() -> None:
    annotations = await _recorded_annotations(redact_sensitive=False)
    assert f"password={_SECRET}" in _curl_description(annotations)


def _annotate_via_route(*, redact_sensitive: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = default_server_config()
        cfg.auth.mode = "header"
        cfg.auth.header_mode_acknowledged = True
        cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
        cfg.recording.enabled_by_default = True
        cfg.recording.redact_sensitive = redact_sensitive
        cfg.recording.directory = Path(tmpdir)  # type: ignore[assignment]

        with TestClient(create_server_app(cfg)) as client:
            client.post("/api/sessions/provide-shell/connect")
            r = client.post(
                "/api/sessions/provide-shell/annotate",
                json={"label": "rotate", "description": f"rotated password={_SECRET} today", "severity": "info"},
            )
            assert r.status_code == 200
            r2 = client.get("/api/sessions/provide-shell/recording/entries?event=annotation")
            assert r2.status_code == 200
            entries = [e for e in r2.json() if e.get("event") == "annotation" and e["data"].get("label") == "rotate"]
            assert len(entries) == 1
            data: dict[str, Any] = entries[0]["data"]
            return data


def test_operator_annotation_is_redacted_when_redact_sensitive_on() -> None:
    data = _annotate_via_route(redact_sensitive=True)
    assert data["description"] == "rotated [PASSWORD_REDACTED] today"
    assert data["source"] == "agent"
    assert data["severity"] == "info"


def test_operator_annotation_is_verbatim_when_redact_sensitive_off() -> None:
    data = _annotate_via_route(redact_sensitive=False)
    assert data["description"] == f"rotated password={_SECRET} today"
