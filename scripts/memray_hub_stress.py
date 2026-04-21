#!/usr/bin/env python
"""Memray stress test for TermHub lifecycle and event ring buffer management."""

import asyncio
from typing import Any

from provide.terminal.bridge.hub import TermHub


class _NoopWs:
    """Minimal WebSocket stub that discards sends without MagicMock overhead."""

    __slots__ = ("_id",)

    def __init__(self, idx: int) -> None:
        self._id = idx

    async def send_json(self, data: Any) -> None:
        pass

    async def send_text(self, data: str) -> None:
        pass

    def __hash__(self) -> int:
        return self._id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _NoopWs) and self._id == other._id


async def main() -> None:
    """Stress test TermHub with 200 workers and 50 browsers each."""
    hub = TermHub(worker_token="stress-token")  # noqa: S106
    num_workers = 200
    num_browsers_per_worker = 50
    ws_counter = 0

    # Register workers and browsers
    for i in range(num_workers):
        worker_id = f"worker-{i:04d}"
        worker_ws = _NoopWs(ws_counter)
        ws_counter += 1
        await hub.register_worker(worker_id, worker_ws)

        # Fill event deque to maxlen (2000 entries) to stress allocation
        for j in range(hub._event_deque_maxlen):
            await hub.append_event(worker_id, "term", {"data": f"output-{j}"})

        # Register browsers for this worker
        for _j in range(num_browsers_per_worker):
            browser_ws = _NoopWs(ws_counter)
            ws_counter += 1
            await hub.register_browser(worker_id, browser_ws, role="operator")

        # Update snapshot
        await hub.update_last_snapshot(worker_id, {"screen": "A" * 1000, "cursor": (0, 0)})

    # Trigger deallocation by deregistering
    for i in range(num_workers):
        worker_id = f"worker-{i:04d}"
        st = hub._workers.get(worker_id)
        if st is not None:
            worker_ws = st.worker_ws
            await hub.deregister_worker(worker_id, worker_ws)
            for browser_ws in list(st.browsers.keys()):
                await hub.cleanup_browser_disconnect(worker_id, browser_ws, owned_hijack=False)
            await hub.prune_if_idle(worker_id)


if __name__ == "__main__":
    asyncio.run(main())
