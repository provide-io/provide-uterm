#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the session runtime's fetch path.

Two admission decisions, recorded by driving the real code.

* **Which session a request is for.** The Durable Object's own identity comes
  back as ``default`` on the Cloudflare Python runtime, so the worker id is
  recovered from the URL path instead. A path segment is taken only when it is
  non-empty and carries no slash of its own — a segment that could contain one
  would let a request name a different session than the one the path appears
  to address.
* **Who may redeem a tunnel invite.** That route is emitted only by the Worker
  when it proxies ``/s/{id}``, and is deliberately absent from the public
  route table. Three things must hold or the answer is 404: the internal
  provenance header must match, the session id must be well-formed, and it
  must be *this* session. Any one of them missing means somebody found the
  route rather than being sent to it.

# uv-package: provide-uterm-cloudflare

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_sessionfetch_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path

from provide.uterm.cloudflare.do.session_runtime import fetch as session_fetch

OUT = Path(__file__).resolve().parent / "sessionfetch_golden.json"


class FakeRequest:
    def __init__(self, url: str) -> None:
        self.url = url


class BrokenUrl:
    """A URL that cannot be read, which the reference tolerates."""

    def __str__(self) -> str:
        raise RuntimeError("no url")


class Runtime:
    """Only what ``_lazy_init_worker_id`` touches."""

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def _restore_state(self) -> None:
        """Cold-runtime state restore; a no-op here — the corpus pins only id resolution."""


PATHS: list[tuple[str, str, str]] = [
    ("a browser socket", "default", "https://x.example/ws/browser/sess-1"),
    ("a worker socket", "default", "https://x.example/ws/worker/sess-1"),
    ("a raw socket", "default", "https://x.example/ws/raw/sess-1"),
    ("a tunnel", "default", "https://x.example/tunnel/sess-1"),
    ("a worker route", "default", "https://x.example/worker/sess-1"),
    ("a session API route", "default", "https://x.example/api/sessions/sess-1"),
    ("a session API route with more after it", "default", "https://x.example/api/sessions/sess-1/hijack"),
    ("an invite redemption", "default", "https://x.example/_internal/tunnel-invite/sess-1/redeem"),
    ("a path with a query on it", "default", "https://x.example/ws/browser/sess-1?token=x"),
    ("a path with a fragment on it", "default", "https://x.example/ws/browser/sess-1#frag"),
    ("an encoded session id", "default", "https://x.example/ws/browser/sess%2D1"),
    ("an encoded slash in an invite redemption", "default", "https://x.example/_internal/tunnel-invite/a%2Fb/redeem"),
    (
        "an encoded session id in an invite redemption",
        "default",
        "https://x.example/_internal/tunnel-invite/sess%2D1/redeem",
    ),
    ("an encoded slash in a socket path", "default", "https://x.example/ws/browser/a%2Fb"),
    ("an empty segment", "default", "https://x.example/ws/browser/"),
    ("a prefix and nothing else", "default", "https://x.example/ws/browser"),
    ("a path nobody routes", "default", "https://x.example/healthz"),
    ("the root", "default", "https://x.example/"),
    ("a runtime that already knows its id", "sess-9", "https://x.example/ws/browser/sess-1"),
    ("a redemption with too few parts", "default", "https://x.example/_internal/tunnel-invite/redeem"),
    ("a redemption with too many parts", "default", "https://x.example/_internal/tunnel-invite/a/b/redeem"),
    (
        "something that only looks like a redemption",
        "default",
        "https://x.example/_internal/tunnel-invite/sess-1/redeems",
    ),
    ("a redemption with an empty session id", "default", "https://x.example/_internal/tunnel-invite//redeem"),
    (
        "a redemption with something after the verb",
        "default",
        "https://x.example/_internal/tunnel-invite/a/redeem/extra",
    ),
]


def _lazy(worker_id: str, url: str) -> str:
    runtime = Runtime(worker_id)
    session_fetch._FetchMixin._lazy_init_worker_id(runtime, FakeRequest(url))
    return runtime.worker_id


def _lazy_broken() -> str:
    runtime = Runtime("default")
    request = FakeRequest("")
    request.url = BrokenUrl()  # type: ignore[assignment]
    session_fetch._FetchMixin._lazy_init_worker_id(runtime, request)
    return runtime.worker_id


# provenance, session id in path, this runtime's worker id
REDEEM_CASES: list[tuple[str, str | None, str, str]] = [
    ("everything in order", session_fetch._INVITE_REDEEM_PROVENANCE, "sess-1", "sess-1"),
    ("no provenance header", None, "sess-1", "sess-1"),
    ("the wrong provenance", "browser", "sess-1", "sess-1"),
    ("an empty provenance", "", "sess-1", "sess-1"),
    ("a session that is not this one", session_fetch._INVITE_REDEEM_PROVENANCE, "sess-2", "sess-1"),
    ("an empty session id", session_fetch._INVITE_REDEEM_PROVENANCE, "", "sess-1"),
    ("a session id with a slash in it", session_fetch._INVITE_REDEEM_PROVENANCE, "a/b", "sess-1"),
    ("an empty session id on a session with no id", session_fetch._INVITE_REDEEM_PROVENANCE, "", ""),
    ("a slashed session id matching a slashed session", session_fetch._INVITE_REDEEM_PROVENANCE, "a/b", "a/b"),
]


def main() -> None:
    corpus = {
        "invite_prefix": session_fetch._INVITE_REDEEM_PREFIX,
        "invite_header": session_fetch._INVITE_REDEEM_HEADER,
        "invite_provenance": session_fetch._INVITE_REDEEM_PROVENANCE,
        "paths": [
            {"name": name, "worker_id": worker_id, "url": url, "resolved": _lazy(worker_id, url)}
            for name, worker_id, url in PATHS
        ],
        "unreadable_url": _lazy_broken(),
        # The redeem guard's own arithmetic, transcribed from the three
        # conditions in ``_fetch_impl`` — recorded so the port cannot quietly
        # drop one of them.
        "redeem": [
            {
                "name": name,
                "provenance": provenance,
                "session_id": session_id,
                "worker_id": worker_id,
                "allowed": (
                    provenance == session_fetch._INVITE_REDEEM_PROVENANCE
                    and bool(session_id)
                    and "/" not in session_id
                    and session_id == worker_id
                ),
            }
            for name, provenance, session_id, worker_id in REDEEM_CASES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['paths'])} paths)")


if __name__ == "__main__":
    main()
