#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the polling-SSE endpoint.

A Durable Object is evicted when idle and cannot hold a connection open across
that, so server-sent events are delivered by polling: each request returns the
events since a position and closes, and the browser reconnects.

**The position is carried back by the client.** Every event is written with an
``id:`` line, which is what ``EventSource`` echoes in ``Last-Event-ID`` on
reconnect. A client that sends neither that header nor the query parameter
starts from the beginning rather than from wherever the session happens to be,
so nothing is silently skipped.

**A position that cannot be read is the beginning, not a refusal.** Replaying
events a client has already seen is recoverable — they carry their own
sequence numbers — while refusing the request would leave it with no stream at
all.

**A negative position is clamped rather than passed through.** The store's
``seq > ?`` would accept it and return everything, which is the same answer,
but only by accident of the comparison.

**The batch is bounded.** A client returning after a long absence must not be
handed the whole log in one response, so it takes what fits and reconnects for
the rest.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfsse_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.do._sse import _MAX_EVENTS, _RETRY_MS, build_sse_response, route_sse

OUT = Path(__file__).with_name("cfsse_golden.json")

WORKER_ID = "w-session"


class _Store:
    """A store that records what it was asked for and hands back fixed events."""

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = events if events is not None else []
        self.calls: list[tuple[str, int, int]] = []

    def list_events_since(self, worker_id: str, after_seq: int, limit: int) -> list[dict[str, Any]]:
        self.calls.append((worker_id, after_seq, limit))
        return self.events


class _Runtime:
    """The smallest runtime the route reads."""

    def __init__(self, store: _Store, worker_id: str = WORKER_ID) -> None:
        self.store = store
        self.worker_id = worker_id


class _Request:
    """A request carrying headers, or not carrying them at all."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        if headers is not None:
            self.headers = headers


# Events as the store hands them back.
SAMPLE_EVENTS: list[dict[str, Any]] = [
    {"seq": 42, "ts": 1_700_000_000.25, "type": "snapshot", "payload": {"rows": 24}},
    {"seq": 43, "ts": 1_700_000_001.5, "type": "input_send", "payload": {"data": "ls\r"}},
]

# A timestamp that happens to land on a whole second. Python renders it
# ``1700000000.0`` because the value is a float; ECMAScript has no such type
# and renders ``1700000000``. Recorded so the divergence is pinned rather than
# found later in a client that reads the field back.
WHOLE_FLOAT_EVENT: dict[str, Any] = {"seq": 1, "ts": 1_700_000_000.0}

# (name, events) — what the body looks like.
BODY_CASES: list[tuple[str, list[dict[str, Any]]]] = [
    ("a batch of events", SAMPLE_EVENTS),
    ("nothing to send", []),
    ("one event", SAMPLE_EVENTS[:1]),
    # An event with no sequence still gets an id line, empty. The client's
    # position would not advance, which is why the store always writes one.
    ("an event with no sequence", [{"type": "tick"}]),
    ("a sequence that is not a number", [{"seq": "x", "type": "tick"}]),
    # The payload is JSON inside an SSE data line, so a newline in it would
    # split the event in two if it were not escaped.
    ("a payload containing a newline", [{"seq": 1, "data": "a\nb"}]),
    ("a payload containing non-ascii", [{"seq": 2, "data": "héllo → ✓"}]),
]

# (name, url, headers) — where the client says it got to.
POSITION_CASES: list[tuple[str, str, dict[str, str] | None]] = [
    ("a query parameter", f"https://h/api/sessions/{WORKER_ID}/events/stream?after_seq=41", None),
    ("the first request", f"https://h/api/sessions/{WORKER_ID}/events/stream?after_seq=0", None),
    ("no position at all", f"https://h/api/sessions/{WORKER_ID}/events/stream", None),
    ("the reconnect header", "https://h/s", {"last-event-id": "17"}),
    # The parameter is what this request asked for; the header is what the
    # last one ended at.
    ("both, the parameter winning", "https://h/s?after_seq=5", {"last-event-id": "17"}),
    ("an empty parameter falling back to the header", "https://h/s?after_seq=", {"last-event-id": "17"}),
    ("an empty parameter and no header", "https://h/s?after_seq=", None),
    ("an empty header", "https://h/s", {"last-event-id": ""}),
    ("a request with no headers at all", "https://h/s", None),
    ("a negative position", "https://h/s?after_seq=-5", None),
    ("a position that is not a number", "https://h/s?after_seq=abc", None),
    ("a fractional position", "https://h/s?after_seq=1.5", None),
    ("a header that is not a number", "https://h/s", {"last-event-id": "abc"}),
    ("a negative header", "https://h/s", {"last-event-id": "-9"}),
    ("a position with spaces around it", "https://h/s?after_seq=%207%20", None),
    ("a signed position", "https://h/s?after_seq=%2B7", None),
    ("a position repeated", "https://h/s?after_seq=3&after_seq=9", None),
    ("other parameters alongside", "https://h/s?foo=bar&after_seq=8&baz=1", None),
    ("a position after a fragment marker", "https://h/s?after_seq=4#after_seq=99", None),
    # Everything after the marker is the fragment, so the query never starts.
    # A port that looks for the marker only after the question mark reads this
    # as a position.
    ("a query inside a fragment", "https://h/s#frag?after_seq=9", None),
    ("a parameter whose name merely starts the same", "https://h/s?after_seqx=5&after_seq=9", None),
    # The query begins at the *first* question mark; a second one is part of a
    # value. Splitting at the last would read the tail as the whole query.
    ("a second question mark", "https://h/s?a=1?after_seq=9", None),
    # A pair with no value is dropped, so the real parameter behind it is
    # still found. A port that reads a bare name as a name one character
    # shorter would match this one and stop here.
    ("a bare name that is nearly the parameter", "https://h/s?after_seqs&after_seq=9", None),
    # The pair splits at its *first* equals sign, so this is the parameter
    # carrying an unreadable value — not some other parameter, and the second
    # occurrence does not get a turn.
    ("a value containing an equals sign", "https://h/s?after_seq=5=6&after_seq=9", None),
    # ``unquote_plus`` turns it into a space, which ``int`` then strips. Left
    # as a plus it is not a trailing sign and the position is unreadable.
    ("a trailing plus", "https://h/s?after_seq=7+", None),
    ("a query on a relative url", "/api/sessions/x/events/stream?after_seq=6", None),
    ("the largest position both runtimes hold exactly", "https://h/s?after_seq=9007199254740991", None),
]

# Beyond that, Python keeps the integer exactly and ECMAScript rounds it to
# the nearest double. Both then ask the store for events after a sequence far
# larger than any that exists, so the answer is the same — but the number
# asked for is not, and pretending otherwise would hide it.
BEYOND_EXACT_URL = "https://h/s?after_seq=99999999999999999999"


async def _body(events: list[dict[str, Any]]) -> str:
    """What the response body carries."""
    response = build_sse_response(events)
    return str(response.body)


async def _headers(events: list[dict[str, Any]]) -> dict[str, str]:
    """What the response headers carry."""
    return dict(build_sse_response(events).headers)


async def _position(url: str, headers: dict[str, str] | None) -> dict[str, Any]:
    """What the route asked the store for."""
    store = _Store()
    await route_sse(_Runtime(store), _Request(headers), url, WORKER_ID)
    worker_id, after_seq, limit = store.calls[0]
    return {"worker_id": worker_id, "after_seq": after_seq, "limit": limit}


async def _build() -> dict[str, Any]:
    """Everything the endpoint decides."""
    # A request for a session this object is not.
    other = _Store()
    wrong = await route_sse(_Runtime(other), _Request(), "https://h/s", "somebody-else")

    matching_store = _Store(SAMPLE_EVENTS)
    matching = await route_sse(_Runtime(matching_store), _Request(), "https://h/s?after_seq=41", WORKER_ID)

    return {
        "retry_ms": _RETRY_MS,
        "max_events": _MAX_EVENTS,
        "worker_id": WORKER_ID,
        "bodies": [{"name": name, "events": events, "body": await _body(events)} for name, events in BODY_CASES],
        "headers": await _headers(SAMPLE_EVENTS),
        "status": build_sse_response(SAMPLE_EVENTS).status,
        # A different retry interval, which the caller may set.
        "custom_retry_body": str(build_sse_response([], retry_ms=500).body),
        "positions": [
            {"name": name, "url": url, "headers": headers, "asked": await _position(url, headers)}
            for name, url, headers in POSITION_CASES
        ],
        # Divergences, recorded rather than left to be discovered.
        "whole_float_event": WHOLE_FLOAT_EVENT,
        "whole_float_body": await _body([WHOLE_FLOAT_EVENT]),
        "beyond_exact_url": BEYOND_EXACT_URL,
        # A string, because JSON cannot carry it back into ECMAScript intact.
        "beyond_exact_after_seq": str((await _position(BEYOND_EXACT_URL, None))["after_seq"]),
        "wrong_session": {
            "status": wrong.status,
            "body": str(wrong.body),
            "headers": dict(wrong.headers),
            # The store is never touched for a session this object is not.
            "store_calls": len(other.calls),
        },
        "matching_session": {
            "status": matching.status,
            "body": str(matching.body),
            "asked": list(matching_store.calls[0]),
        },
    }


def main() -> int:
    """Write the golden corpus and report what it covers."""
    corpus = asyncio.run(_build())
    # Not sorted, unlike the other corpora here. The reference writes an
    # event's fields in the order it received them, and sorting the corpus
    # would sort the recorded events too — leaving the body strings pinned
    # against events whose order had already been destroyed. Insertion order
    # is deterministic, so the file is still stable.
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(BODY_CASES)} bodies, {len(POSITION_CASES)} positions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
