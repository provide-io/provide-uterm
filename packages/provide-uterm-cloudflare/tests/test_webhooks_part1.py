#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for do/_webhooks.py, state/store.py webhook methods, and dispatch."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

from provide.uterm.cloudflare.state.store import SqliteStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store() -> SqliteStateStore:
    conn = sqlite3.connect(":memory:")
    store = SqliteStateStore(conn.execute)
    store.migrate()
    return store


class _Runtime:
    def __init__(self, store: SqliteStateStore, worker_id: str = "w1") -> None:
        self.store = store
        self.worker_id = worker_id
        self.meta: dict = {
            "display_name": self.worker_id,
            "connector_type": "unknown",
            "created_at": 0.0,
            "tags": [],
            "visibility": "public",
            "owner": None,
        }

    async def request_json(self, request: object) -> dict[str, Any]:
        return json.loads(getattr(request, "_body", "{}"))


def _req(url: str, *, method: str = "GET", body: dict | None = None) -> SimpleNamespace:
    ns = SimpleNamespace(url=url, method=method)
    ns._body = json.dumps(body) if body else "{}"
    return ns


# ---------------------------------------------------------------------------
# Store: webhook CRUD
# ---------------------------------------------------------------------------


def test_store_save_and_load_webhook() -> None:
    store = _make_store()
    store.save_webhook("wh1", "s1", "https://example.com/hook")
    webhooks = store.load_webhooks("s1")
    assert len(webhooks) == 1
    assert webhooks[0]["webhook_id"] == "wh1"
    assert webhooks[0]["url"] == "https://example.com/hook"
    assert webhooks[0]["event_types"] is None
    assert webhooks[0]["pattern"] is None
    assert webhooks[0]["secret"] is None


def test_store_save_webhook_with_options() -> None:
    store = _make_store()
    store.save_webhook("wh2", "s1", "https://example.com/hook", event_types=["snapshot"], pattern=r"\$", secret="sec")
    webhooks = store.load_webhooks("s1")
    assert webhooks[0]["event_types"] == ["snapshot"]
    assert webhooks[0]["pattern"] == r"\$"
    assert webhooks[0]["secret"] == "sec"


def test_store_load_webhooks_session_isolation() -> None:
    store = _make_store()
    store.save_webhook("wh1", "s1", "https://example.com/a")
    store.save_webhook("wh2", "s2", "https://example.com/b")
    assert len(store.load_webhooks("s1")) == 1
    assert len(store.load_webhooks("s2")) == 1
    assert len(store.load_webhooks("s3")) == 0


def test_store_delete_webhook_existing() -> None:
    store = _make_store()
    store.save_webhook("wh1", "s1", "https://example.com/hook")
    result = store.delete_webhook("wh1")
    assert result is True
    assert store.load_webhooks("s1") == []


def test_store_delete_webhook_not_found() -> None:
    store = _make_store()
    result = store.delete_webhook("nonexistent")
    assert result is False
