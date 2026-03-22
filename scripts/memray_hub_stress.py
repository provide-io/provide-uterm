#!/usr/bin/env python
"""Memray stress test for TermHub lifecycle and event ring buffer management."""

import asyncio
from unittest.mock import MagicMock

from provide.terminal.hijack.hub import TermHub


async def main() -> None:
    """Stress test TermHub with 200 workers and 50 browsers each."""
    hub = TermHub(worker_token="stress-token")
    num_workers = 200
    num_browsers_per_worker = 50

    # Register workers and browsers
    for i in range(num_workers):
        worker_id = f"worker-{i:04d}"
        mock_ws = MagicMock()
        await hub.register_worker(worker_id, mock_ws)

        # Fill event deque to maxlen (2000 entries) to stress allocation
        for j in range(hub._event_deque_maxlen):
            await hub.append_event(worker_id, "term", {"data": f"output-{j}"})

        # Register browsers for this worker
        for j in range(num_browsers_per_worker):
            browser_ws = MagicMock()
            await hub.register_browser(worker_id, browser_ws, role="operator")

        # Update snapshot
        await hub.update_last_snapshot(worker_id, {"screen": "A" * 1000, "cursor": (0, 0)})

    # Trigger deallocation by deregistering
    for i in range(num_workers):
        worker_id = f"worker-{i:04d}"
        st = hub._workers.get(worker_id)
        if st is not None:
            mock_ws = st.worker_ws
            await hub.deregister_worker(worker_id, mock_ws)
            for browser_ws in list(st.browsers.keys()):
                await hub.cleanup_browser_disconnect(worker_id, browser_ws, owned_hijack=False)
            await hub.prune_if_idle(worker_id)


if __name__ == "__main__":
    asyncio.run(main())
