#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Library-level session recording demo (Python) — screen snapshots → JSONL."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from provide.uterm.recording import LocalFileRecordingStore

_MAGENTA = "\033[1;35m"
_GREEN = "\033[1;32m"
_CYAN = "\033[1;36m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"


def banner(title: str) -> None:
    bar = "═" * (len(title) + 4)
    print(f"\n{_MAGENTA}{bar}{_RESET}", flush=True)
    print(f"{_MAGENTA}  {_BOLD}{title}{_RESET}{_MAGENTA}  {_RESET}", flush=True)
    print(f"{_MAGENTA}{bar}{_RESET}\n", flush=True)


def info(msg: str) -> None:
    print(f"{_CYAN}  → {msg}{_RESET}", flush=True)


def ok(msg: str) -> None:
    print(f"{_GREEN}  ✓ {msg}{_RESET}", flush=True)


def kv(key: str, value: object) -> None:
    print(f"    {_DIM}{key}:{_RESET} {_BOLD}{value}{_RESET}", flush=True)


async def main() -> None:
    banner("provide-uterm recording — Python")
    info("language=python  store=LocalFileRecordingStore")

    with tempfile.TemporaryDirectory(prefix="uterm-rec-py-") as tmp:
        store = LocalFileRecordingStore(tmp)
        sid = "demo-recording-py"
        await store.start_session(
            sid,
            {
                "lang": "python",
                "feature": "session_recording",
                "demo": "recording_matrix",
            },
        )
        ok(f"session started: {sid}")

        screens = [
            "",
            "=== provide-uterm: session recording active ===\n",
            "=== provide-uterm: session recording active ===\n[deploy] step 1: pulling config\n",
            "=== provide-uterm: session recording active ===\n"
            "[deploy] step 1: pulling config\n"
            "[deploy] step 2: running migrations\n",
            "=== provide-uterm: session recording active ===\n"
            "[deploy] step 1: pulling config\n"
            "[deploy] step 2: running migrations\n"
            "[deploy] step 3: restarting services\n",
            "=== provide-uterm: session recording active ===\n"
            "[deploy] step 1: pulling config\n"
            "[deploy] step 2: running migrations\n"
            "[deploy] step 3: restarting services\n"
            "[deploy] healthcheck: ok — recording complete\n",
        ]

        for i, screen in enumerate(screens):
            events = [
                {
                    "ts": time.time(),
                    "event": "snapshot",
                    "session_id": sid,
                    "data": {
                        "seq": i,
                        "screen": screen,
                        "cols": 80,
                        "rows": 24,
                        "source": "python",
                    },
                }
            ]
            await store.append_events(sid, events)
            info(f"snapshot {i}: {len(screen)} screen bytes")
            await asyncio.sleep(0.15)

        await store.end_session(sid)
        meta = await store.recording_meta(sid)
        path = await store.get_path(sid)
        entries = await store.get_entries(sid, limit=50)
        kv("exists", meta.get("exists"))
        kv("size_bytes", meta.get("size_bytes"))
        kv("path", str(path) if path else "")
        kv("entries", len(entries))
        snapshots = [e for e in entries if e.get("event") == "snapshot"]
        kv("snapshots", len(snapshots))
        if path and Path(path).is_file():
            sample = Path(path).read_text(encoding="utf-8").splitlines()[:2]
            for line in sample:
                info(f"jsonl: {line[:100]}{'…' if len(line) > 100 else ''}")
        ok("Python LocalFileRecordingStore: screen snapshots persisted as JSONL")


if __name__ == "__main__":
    asyncio.run(main())
