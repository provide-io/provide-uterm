#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# uv-package: provide-uterm-cloudflare
"""Generate the differential golden corpus for the KV session registry.

Every Durable Object writes its own status here, and the Default Worker reads
the lot back to answer "what sessions exist". Two things about that are
load-bearing.

**The write is a read-modify-write, not an overwrite.** The tunnel API keeps
credential hashes, a revoked flag, expiry and one-time invites in this same
key, and the Durable Object rewrites its status every sixty seconds. A blind
put nulled tunnel, share and control auth about a minute after every worker
reconnect — the status fields are merged *over* whatever is already there so
create, revoke and rotate stay authoritative.

**The fleet list is redacted.** Token material and invite secrets live in the
same document, because a Durable Object needs them to bootstrap a tunnel.
Listing sessions must not hand them out — during an invite window that would
be a long-lived credential in an unauthenticated-ish list response.

Everything degrades rather than raising. KV is a network call: a status write
that failed because the store was briefly unreachable must not take the
session down with it, and a corrupt entry must not break the whole listing.

Usage (from the repository root)::

    uv run --package provide-uterm-cloudflare python \\
        packages/provide-uterm-ts/testdata/gen_cfregistry_golden.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from provide.uterm.cloudflare.state.registry import (
    delete_kv_session,
    get_kv_session,
    list_kv_sessions,
    update_kv_session,
)

OUT = Path(__file__).with_name("cfregistry_golden.json")


class _KV:
    """A key-value store that records what it was asked to do."""

    def __init__(self, initial: dict[str, str] | None = None, fail: str | None = None) -> None:
        self.data: dict[str, str] = dict(initial or {})
        self.fail = fail
        self.calls: list[str] = []

    async def get(self, key: str) -> str | None:
        self.calls.append(f"get:{key}")
        if self.fail == "get":
            raise RuntimeError("kv unreachable")
        return self.data.get(key)

    async def put(self, key: str, value: str) -> None:
        self.calls.append(f"put:{key}")
        if self.fail == "put":
            raise RuntimeError("kv unreachable")
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.calls.append(f"delete:{key}")
        if self.fail == "delete":
            raise RuntimeError("kv unreachable")
        self.data.pop(key, None)

    async def list(self, prefix: str = "") -> Any:
        self.calls.append(f"list:{prefix}")
        if self.fail == "list":
            raise RuntimeError("kv unreachable")
        # An object whose ``keys`` is a list, which is the shape the real
        # binding hands back. A plain dict does not work here — see
        # ``python_rejects_a_dict_listing`` below.
        return _Listing([{"name": key} for key in sorted(self.data) if key.startswith(prefix)])


class _Listing:
    """What a KV list call returns: an object whose ``keys`` is a list."""

    def __init__(self, keys: list[Any]) -> None:
        self.keys = keys


class _Env:
    """A Worker environment carrying a registry binding."""

    def __init__(self, kv: Any) -> None:
        self.SESSION_REGISTRY = kv


class _NoBinding:
    """A Worker environment with no registry configured."""


# An entry as the tunnel API leaves it: status fields plus credential material.
EXISTING = {
    "session_id": "w1",
    "display_name": "the old name",
    "created_at": 1000.0,
    "connector_type": "telnet",
    "connected": True,
    "owner": "alice",
    "visibility": "private",
    "tags": ["a"],
    "share_token_hash": "sha256:abc",
    "control_token_hash": "sha256:def",
    "share_invite_token": "one-time-share",
    "control_invite_token": "one-time-control",
    "worker_token": "wt",
    "revoked": False,
    "expires_at": 2000.0,
}


async def _record_updates() -> list[dict[str, Any]]:
    """What a status write leaves in the store."""
    records: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("a fresh session", {}, {"connected": True}),
        (
            "with everything the meta can carry",
            {},
            {
                "connected": True,
                "hijacked": True,
                "input_mode": "observe",
                "recording_enabled": False,
                "recording_available": True,
                "meta": {
                    "display_name": "Session One",
                    "created_at": 1234.5,
                    "connector_type": "ssh",
                    "tags": ["x", "y"],
                    "owner": "bob",
                    "visibility": "private",
                },
            },
        ),
        ("an empty meta", {}, {"connected": True, "meta": {}}),
        (
            "a meta whose fields are empty",
            {},
            {"connected": True, "meta": {"display_name": "", "connector_type": "", "tags": [], "visibility": ""}},
        ),
        # The one that matters: a status write over an entry the tunnel API owns.
        ("over an existing entry", EXISTING, {"connected": True}),
        ("over an existing entry while disconnecting", EXISTING, {"connected": False, "remove_offline": False}),
        # connected=None means "leave it as it was".
        ("inheriting the connected flag", EXISTING, {"connected": None}),
        ("inheriting from an entry that has none", {"session_id": "w1"}, {"connected": None}),
        ("disconnecting removes the entry", EXISTING, {"connected": False}),
        ("disconnecting without removing", EXISTING, {"connected": False, "remove_offline": False}),
    ]
    out = []
    for name, initial, kwargs in records:
        kv = _KV({"session:w1": json.dumps(initial)} if initial else {})
        await update_kv_session(_Env(kv), "w1", **kwargs)
        stored = kv.data.get("session:w1")
        out.append(
            {
                "name": name,
                "initial": initial or None,
                "kwargs": dict(kwargs),
                "stored": json.loads(stored) if stored else None,
                "calls": kv.calls,
            }
        )
    return out


async def _record_degradation() -> dict[str, Any]:
    """What happens when the store is unreachable or the data is bad."""
    put_fails = _KV(fail="put")
    await update_kv_session(_Env(put_fails), "w1", connected=True)

    get_fails = _KV(fail="get")
    await update_kv_session(_Env(get_fails), "w1", connected=True)

    delete_fails = _KV(fail="delete")
    await update_kv_session(_Env(delete_fails), "w1", connected=False)

    corrupt = _KV({"session:w1": "{not json"})
    await update_kv_session(_Env(corrupt), "w1", connected=True)

    not_an_object = _KV({"session:w1": '["a list"]'})
    await update_kv_session(_Env(not_an_object), "w1", connected=True)

    return {
        "put_failure_is_silent": True,
        "get_failure_still_writes": json.loads(get_fails.data["session:w1"]),
        "delete_failure_is_silent": True,
        "corrupt_entry_is_replaced": json.loads(corrupt.data["session:w1"]),
        "non_object_entry_is_replaced": json.loads(not_an_object.data["session:w1"]),
        "put_failure_left_nothing": "session:w1" not in put_fails.data,
        "delete_failure_left_it": "session:w1" not in delete_fails.data,
    }


async def _record_reads() -> dict[str, Any]:
    """Reading one entry and listing them all."""
    populated = _KV(
        {
            "session:w1": json.dumps(EXISTING),
            "session:w2": json.dumps({"session_id": "w2", "connected": False}),
            "other:thing": json.dumps({"not": "a session"}),
            "session:broken": "{not json",
            "session:list": '["not an object"]',
        }
    )
    env = _Env(populated)

    listed = await list_kv_sessions(env)
    one = await get_kv_session(env, "w1")
    missing = await get_kv_session(env, "nobody")

    deleted_env = _Env(_KV({"session:w1": json.dumps(EXISTING)}))
    await delete_kv_session(deleted_env, "w1")
    after_delete = await get_kv_session(deleted_env, "w1")

    get_fails = _Env(_KV({"session:w1": json.dumps(EXISTING)}, fail="get"))
    list_fails = _Env(_KV({"session:w1": json.dumps(EXISTING)}, fail="list"))
    delete_fails = _Env(_KV({"session:w1": json.dumps(EXISTING)}, fail="delete"))
    await delete_kv_session(delete_fails, "w1")

    return {
        "listed": listed,
        "one": one,
        "missing": missing,
        "after_delete": after_delete,
        "get_failure": await get_kv_session(get_fails, "w1"),
        "list_failure": await list_kv_sessions(list_fails),
        "delete_failure_is_silent": True,
    }


async def _record_no_binding() -> dict[str, Any]:
    """A Worker with no registry configured does nothing, quietly."""
    env = _NoBinding()
    await update_kv_session(env, "w1", connected=True)
    await delete_kv_session(env, "w1")
    return {
        "get": await get_kv_session(env, "w1"),
        "list": await list_kv_sessions(env),
    }


async def _record_list_shapes() -> dict[str, Any]:
    """The two shapes a KV listing can arrive in."""

    class _ObjectKey:
        name = "session:w1"

    class _ObjectKeyKV(_KV):
        async def list(self, prefix: str = "") -> Any:
            return _Listing([_ObjectKey(), {"name": ""}, {"nothing": "here"}])

    class _DictListingKV(_KV):
        async def list(self, prefix: str = "") -> Any:
            return {"keys": [{"name": "session:w1"}]}

    entry = {"session:w1": json.dumps(EXISTING)}

    # The reference reads ``result.keys`` whenever the attribute exists — and
    # every dict has one, as a method. A shim returning a plain dict therefore
    # iterates a bound method and raises out of the listing entirely.
    dict_listing_error: str | None = None
    try:
        await list_kv_sessions(_Env(_DictListingKV(entry)))
    except TypeError as exc:
        dict_listing_error = str(exc)

    return {
        "keys_as_objects": await list_kv_sessions(_Env(_ObjectKeyKV(entry))),
        "python_rejects_a_dict_listing": dict_listing_error,
    }


async def _build() -> dict[str, Any]:
    """Everything, in one event loop."""
    return {
        "existing": EXISTING,
        "updates": await _record_updates(),
        "degradation": await _record_degradation(),
        "reads": await _record_reads(),
        "no_binding": await _record_no_binding(),
        "list_shapes": await _record_list_shapes(),
        "kv_refresh_s": 60,
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = asyncio.run(_build())
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(corpus['updates'])} update cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
