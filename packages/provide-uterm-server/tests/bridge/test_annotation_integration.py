#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Integration tests: PatternDetector wired into HostedSessionRuntime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.terminal.bridge.annotation._detector import PatternDetector
from provide.terminal.server.models import RecordingConfig, SessionDefinition
from provide.terminal.server.runtime import HostedSessionRuntime
from provide.terminal.session_logger import SessionLogger


def _make_session(session_id: str = "ann-test") -> SessionDefinition:
    return SessionDefinition(
        session_id=session_id,
        display_name="Annotation Test",
        connector_type="shell",
        auto_start=False,
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


async def _make_runtime_with_logger(
    tmp_path: Path,
    *,
    detector: PatternDetector | None = None,
) -> HostedSessionRuntime:
    recording_path = tmp_path / "test.jsonl"
    rt = HostedSessionRuntime(
        _make_session(),
        public_base_url="http://localhost:9999",
        recording=RecordingConfig(),
        detector=detector,
    )
    rt._recording_path = recording_path
    rt._logger = SessionLogger(recording_path)
    await rt._logger.start("ann-test")
    return rt


class TestDetectorAnnotatesOnSnapshot:
    async def test_aws_key_triggers_annotation(self, tmp_path: Path) -> None:
        detector = PatternDetector()
        rt = await _make_runtime_with_logger(tmp_path, detector=detector)

        await rt._log_snapshot({"screen": "config: AKIAIOSFODNN7EXAMPLE key found"})
        await rt._logger.stop()

        entries = _read_jsonl(rt._recording_path)  # type: ignore[arg-type]
        annotation_entries = [e for e in entries if e.get("event") == "annotation"]
        assert len(annotation_entries) >= 1
        payload = annotation_entries[0]["data"]
        assert payload["label"] == "credential_exposure"
        assert payload["severity"] == "high"
        assert payload["span"]["from_seq"] == 1
        assert payload["span"]["to_seq"] == 1


class TestDetectorAnnotatesOnSend:
    async def test_sudo_triggers_annotation(self, tmp_path: Path) -> None:
        detector = PatternDetector()
        rt = await _make_runtime_with_logger(tmp_path, detector=detector)

        await rt._log_send("sudo rm -rf /tmp/junk")
        await rt._logger.stop()

        entries = _read_jsonl(rt._recording_path)  # type: ignore[arg-type]
        annotation_entries = [e for e in entries if e.get("event") == "annotation"]
        # Should have at least one for escalation (sudo) and one for destructive (rm -rf)
        labels = {e["data"]["label"] for e in annotation_entries}
        assert "privilege_escalation" in labels


class TestNoDetectorNoAnnotations:
    async def test_no_detector_produces_no_annotations(self, tmp_path: Path) -> None:
        rt = await _make_runtime_with_logger(tmp_path, detector=None)

        await rt._log_snapshot({"screen": "config: AKIAIOSFODNN7EXAMPLE key found"})
        await rt._log_send("sudo rm -rf /tmp/junk")
        await rt._logger.stop()

        entries = _read_jsonl(rt._recording_path)  # type: ignore[arg-type]
        annotation_entries = [e for e in entries if e.get("event") == "annotation"]
        assert annotation_entries == []


class TestNoMatchNoAnnotation:
    async def test_normal_text_no_annotations(self, tmp_path: Path) -> None:
        detector = PatternDetector()
        rt = await _make_runtime_with_logger(tmp_path, detector=detector)

        await rt._log_snapshot({"screen": "user@host:~$ ls -la\ntotal 42\ndrwxr-xr-x"})
        await rt._log_send("ls -la\n")
        await rt._logger.stop()

        entries = _read_jsonl(rt._recording_path)  # type: ignore[arg-type]
        annotation_entries = [e for e in entries if e.get("event") == "annotation"]
        assert annotation_entries == []
