from __future__ import annotations

from provide_terminal_cloudflare.entry import _extract_worker_id


def test_extract_worker_id_for_ws_route() -> None:
    assert _extract_worker_id("/ws/browser/bot1/term") == "bot1"
    assert _extract_worker_id("/ws/worker/bot2/term") == "bot2"
    assert _extract_worker_id("/ws/raw/bot4/term") == "bot4"
    assert _extract_worker_id("/worker/bot3/hijack/acquire") == "bot3"
    assert _extract_worker_id("/unknown") is None
