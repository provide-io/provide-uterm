#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""M3 regression: the events ring buffer must redact content at write time.

The live broadcast path redacts via the output gate, but ``append_event``
previously stored the RAW event in the ring buffer. The events read surfaces
(``/api/sessions/{id}/events``, ``/events/watch``, the MCP events tools) all
read straight from that ring, so they were a single unredacted egress for
everything the broadcast scrubs.

The fix redacts content at WRITE time in ``append_event`` using the
SERVER-DEFAULT ruleset (``default_rules()``) — events are role-agnostic and so
are redacted once. The default redactor is built once and reused.
"""

from __future__ import annotations

from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.bridge.models import WorkerTermState

# A JWT-shaped secret matched by default_rules() (the _JWT pattern). Using a
# real default-ruleset shape proves we redact with the server default set, not
# a test-injected gate.
_JWT_SECRET = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dummsignaturevalue123"  # pragma: allowlist secret
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # matches default _AWS_ACCESS_KEY_ID  # pragma: allowlist secret


async def _register_worker(hub: TermHub, worker_id: str) -> None:
    async with hub._lock:
        hub.registry._workers[worker_id] = WorkerTermState()


async def test_term_event_content_redacted_in_ring() -> None:
    """A term event whose data contains a default-ruleset secret is stored redacted."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    evt = await hub.append_event("w1", "term", {"data": f"login token {_JWT_SECRET} ok"})

    assert _JWT_SECRET not in evt["data"]["data"]
    assert "[JWT_REDACTED]" in evt["data"]["data"]
    # Read it back from the ring via the public read surface.
    recent = await hub.get_recent_events("w1", limit=10)
    ring = [e for e in recent if e["type"] == "term"][-1]
    assert _JWT_SECRET not in ring["data"]["data"]


async def test_snapshot_event_content_redacted_in_ring() -> None:
    """A snapshot event's screen field is redacted in the ring copy."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    await hub.append_event(
        "w1",
        "snapshot",
        {"prompt_id": "p1", "screen_hash": "h1", "screen": f"key {_AWS_KEY} here"},
    )
    recent = await hub.get_recent_events("w1", limit=10)
    ring = [e for e in recent if e["type"] == "snapshot"][-1]
    assert _AWS_KEY not in ring["data"]["screen"]
    assert "[AWS_ACCESS_KEY_REDACTED]" in ring["data"]["screen"]


async def test_analysis_event_content_redacted_in_ring() -> None:
    """An analysis event's formatted and raw fields are redacted in the ring copy."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    await hub.append_event(
        "w1",
        "analysis",
        {"formatted": f"saw {_JWT_SECRET}", "raw": {"detail": _AWS_KEY}},
    )
    recent = await hub.get_recent_events("w1", limit=10)
    ring = [e for e in recent if e["type"] == "analysis"][-1]
    assert _JWT_SECRET not in ring["data"]["formatted"]
    assert "[JWT_REDACTED]" in ring["data"]["formatted"]
    # M4 structured-recursion benefits the ring too: raw dict secret is redacted.
    assert _AWS_KEY not in str(ring["data"]["raw"])
    assert ring["data"]["raw"]["detail"] == "[AWS_ACCESS_KEY_REDACTED]"


async def test_non_secret_event_unchanged() -> None:
    """An event whose content has no secret is stored verbatim (no false-positive mangling)."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    plain = "just some ordinary terminal output, nothing secret"
    evt = await hub.append_event("w1", "term", {"data": plain})
    assert evt["data"]["data"] == plain


async def test_non_content_event_passes_through() -> None:
    """Non-content event types (e.g. hijack_acquired) are not run through redaction."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    evt = await hub.append_event("w1", "hijack_acquired", {"owner": "dashboard_ws"})
    assert evt["data"] == {"owner": "dashboard_ws"}


async def test_default_redactor_built_once_and_reused() -> None:
    """The default event redactor is cached on the router (not rebuilt per event)."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    await hub.append_event("w1", "term", {"data": "a"})
    first = hub.router._event_redactor
    await hub.append_event("w1", "term", {"data": "b"})
    second = hub.router._event_redactor
    assert first is not None
    assert first is second, "default event redactor must be reused, not rebuilt per event"


async def test_term_truncation_still_applied_with_redaction() -> None:
    """The existing term-data char cap still applies alongside redaction."""
    hub = TermHub(max_event_data_chars=300)
    await _register_worker(hub, "w1")

    long_plain = "Z" * 1000
    evt = await hub.append_event("w1", "term", {"data": long_plain})
    assert len(evt["data"]["data"]) == 300


async def test_redaction_applied_before_truncation_boundary() -> None:
    """A secret near the truncation boundary is still redacted (redaction precedes ring store)."""
    hub = TermHub(max_event_data_chars=400)
    await _register_worker(hub, "w1")

    data = f"prefix {_JWT_SECRET} suffix"  # well under the 400 cap
    evt = await hub.append_event("w1", "term", {"data": data})
    assert _JWT_SECRET not in evt["data"]["data"]
    assert "[JWT_REDACTED]" in evt["data"]["data"]


async def test_snapshot_event_prompt_detected_redacted_in_ring() -> None:
    """A snapshot event carrying a prompt_detected dict has its matched text redacted (M3+M4)."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    await hub.append_event(
        "w1",
        "snapshot",
        {"screen": "ok", "screen_hash": "h", "prompt_detected": {"matched": f"login {_AWS_KEY}"}},
    )
    recent = await hub.get_recent_events("w1", limit=10)
    ring = [e for e in recent if e["type"] == "snapshot"][-1]
    assert _AWS_KEY not in str(ring["data"]["prompt_detected"])
    assert ring["data"]["prompt_detected"]["matched"] == "login [AWS_ACCESS_KEY_REDACTED]"


async def test_term_event_non_string_data_stored_verbatim() -> None:
    """A term event whose data is not a string is stored verbatim (no str()-coercion)."""
    hub = TermHub()
    await _register_worker(hub, "w1")

    evt = await hub.append_event("w1", "term", {"data": 42})
    assert evt["data"]["data"] == 42
