#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Rebuild a raw terminal byte-stream from a JSONL session log."""

from __future__ import annotations

import base64
import json
from pathlib import Path


def rebuild_raw_stream(log_path: str | Path, out_path: str | Path) -> None:
    """Concatenate all ``read`` event raw bytes from a JSONL log into a single file.

    Args:
        log_path: Path to the JSONL session log.
        out_path: Destination file for the raw byte stream.
    """
    log_path = Path(log_path)
    out_path = Path(out_path)

    out = bytearray()
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") != "read":
            continue
        raw_b64 = record.get("data", {}).get("raw_bytes_b64", "")
        if raw_b64:
            out.extend(base64.b64decode(raw_b64))

    out_path.write_bytes(out)
