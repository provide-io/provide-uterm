from __future__ import annotations

from provide.terminal.cloudflare.entry.registry import _extract_worker_id


def test_extract_worker_id_for_ws_route() -> None:
    assert _extract_worker_id("/ws/browser/agent1/term") == "agent1"
    assert _extract_worker_id("/ws/worker/agent2/term") == "agent2"
    assert _extract_worker_id("/ws/raw/agent4/term") == "agent4"
    assert _extract_worker_id("/worker/agent3/hijack/acquire") == "agent3"
    assert _extract_worker_id("/unknown") is None
