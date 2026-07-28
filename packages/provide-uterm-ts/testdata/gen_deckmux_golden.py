#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for the TypeScript DeckMux port.

DeckMux is several people watching one terminal, so its presence state is the
one place a *browser* writes into memory the server then hands to everybody
else.

* **Untrusted values are bounded.** A `selection` or a `pin` arrives from a
  browser, is stored, and is re-broadcast verbatim to every joiner. Unbounded,
  that is memory amplification with an audience. A legitimate selection is a
  handful of small integers, so the caps are generous and cheap.
* **A rejected update changes nothing.** Every field is validated before any is
  written, so an oversized `pin` alongside a valid `scroll_line` leaves the
  stored user exactly as it was rather than half-updated.
* **Exactly one owner.** Setting one clears the rest in the same pass; two
  users both believing they hold control is the failure this prevents.
* **Key display.** A three-character escape is matched before its first
  character, or an arrow key renders as an escape symbol followed by two
  letters — which is what the other participants would see typed.

Usage (from the repository root)::

    uv run python packages/provide-uterm-ts/testdata/gen_deckmux_golden.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from provide.uterm.deckmux import _presence as presence_module
from provide.uterm.deckmux._presence import PresenceStore, UserPresence
from provide.uterm.deckmux._protocol import (
    KEY_SYMBOLS,
    MSG_AUTO_TRANSFER_WARNING,
    MSG_CONTROL_REQUEST,
    MSG_CONTROL_TRANSFER,
    MSG_PRESENCE_LEAVE,
    MSG_PRESENCE_SYNC,
    MSG_PRESENCE_UPDATE,
    MSG_QUEUED_INPUT,
    encode_keys_display,
    make_control_transfer,
    make_presence_leave,
    make_presence_sync,
    make_presence_update,
)

OUT = Path(__file__).with_name("deckmux_golden.json")

NOW = 1_000.0

# (name, raw) — what the other participants see of somebody's typing.
KEY_CASES: list[tuple[str, str]] = [
    ("plain text", "ls -la"),
    ("an arrow key", "\x1b[A"),
    ("every arrow", "\x1b[A\x1b[B\x1b[C\x1b[D"),
    ("a return", "\r"),
    ("a newline", "\n"),
    ("a tab", "\t"),
    ("a delete", "\x7f"),
    ("a backspace", "\x08"),
    ("a bare escape", "\x1b"),
    ("an escape that is not a sequence", "\x1bZ"),
    ("text around an arrow", "ab\x1b[Ccd"),
    ("a truncated escape at the end", "ab\x1b["),
    ("a control character with no symbol", "\x01"),
    ("empty", ""),
    ("a space", " "),
    ("unicode", "héllo → ✓"),
]

# (name, field, value) — the untrusted values a browser can send.
VALIDATION_CASES: list[tuple[str, str, Any]] = [
    ("a real selection", "selection", {"start": {"row": 1, "col": 2}, "end": {"row": 3, "col": 4}}),
    ("a real pin", "pin", {"row": 5, "col": 6}),
    ("clearing it", "selection", None),
    ("not a dict at all", "selection", "everything"),
    ("a list", "pin", [1, 2, 3]),
    ("a number", "pin", 42),
    ("exactly the key limit", "selection", {str(index): index for index in range(16)}),
    ("one key too many", "selection", {str(index): index for index in range(17)}),
    ("just under the byte limit", "pin", {"a": "x" * 2000}),
    ("over the byte limit", "pin", {"a": "x" * 4000}),
    # Either side of the bound, so an off-by-anything is caught rather than
    # only an off-by-a-lot.
    ("exactly at the byte limit", "pin", {"a": "x" * 2039}),
    ("one byte over the limit", "pin", {"a": "x" * 2040}),
    ("something that is not json", "pin", {"when": object()}),
    # A nested list is legitimate — a selection is a pair of coordinates —
    # and it counts towards the size like anything else.
    ("a nested list", "selection", {"range": [1, 2, 3]}),
    ("a nested object", "selection", {"start": {"row": 1}}),
    ("a nested list of objects", "selection", {"spans": [{"row": 1}, {"row": 2}]}),
]


def _failure(call: Any) -> str | None:
    """Run `call` and return the refusal, or None."""
    try:
        call()
    except ValueError as exc:
        return str(exc)
    return None


def _store_with(user_id: str = "u1") -> PresenceStore:
    """A store holding one user."""
    store = PresenceStore()
    store.add(user_id, "Alice", "#ff0000", "operator", initials="AL")
    return store


def _record_validation() -> list[dict[str, Any]]:
    """What each untrusted value does when it is stored."""
    records = []
    for name, field, value in VALIDATION_CASES:
        store = _store_with()
        error = _failure(lambda s=store, f=field, v=value: s.update("u1", **{f: v}))
        stored = getattr(store.get("u1"), field)
        records.append(
            {
                "name": name,
                "field": field,
                # ``object()`` has no JSON form; the case is about the refusal.
                "value": value if name != "something that is not json" else "<unserialisable>",
                "error": error,
                "stored_is_unchanged": stored is None,
            }
        )
    return records


def _record_partial_update() -> dict[str, Any]:
    """A rejected field must not leave the rest of the update applied."""
    store = _store_with()
    error = _failure(lambda: store.update("u1", scroll_line=42, pin={"a": "x" * 4000}))
    user = store.get("u1")
    return {
        "error": error,
        "scroll_line_after": user.scroll_line if user else None,
        "pin_after": user.pin if user else None,
    }


def _record_store() -> dict[str, Any]:
    """The store's own behaviour."""
    store = PresenceStore()
    store.add("u1", "Alice", "#ff0000", "operator", initials="AL")
    store.add("u2", "Bob", "#00ff00", "viewer")

    store.set_owner("u1")
    first_owner = store.get_owner()
    store.set_owner("u2")
    second_owner = store.get_owner()
    owners_after_set = [p.user_id for p in store.get_all() if p.is_owner]
    store.clear_owner()
    cleared = store.get_owner()

    return {
        "count": store.count,
        "colors": sorted(store.taken_colors()),
        "first_owner": first_owner.user_id if first_owner else None,
        "second_owner": second_owner.user_id if second_owner else None,
        "owners_after_set": owners_after_set,
        "owner_after_clear": cleared,
        "unknown_field": _failure(lambda: store.update("u1", nonsense=1)),
        "update_a_user_that_is_not_there": store.update("nobody", typing=True),
        "remove_returns_the_user": store.remove("u2").user_id,  # type: ignore[union-attr]
        "remove_a_user_that_is_not_there": store.remove("nobody"),
        "count_after_remove": store.count,
        "get_a_user_that_is_not_there": store.get("nobody"),
    }


def _record_idle() -> dict[str, Any]:
    """Who the pruner takes."""
    real_time = time.time
    time.time = lambda: NOW  # type: ignore[assignment]
    try:
        store = PresenceStore()
        store.add("fresh", "A", "#f00", "viewer")
        store.add("stale", "B", "#0f0", "viewer")
        stale = store.get("stale")
        assert stale is not None
        stale.last_activity_at = NOW - 100
        on_the_line = store.add("exact", "C", "#00f", "viewer")
        on_the_line.last_activity_at = NOW - 60

        idle_states = {
            "fresh": store.get("fresh").is_idle(60),  # type: ignore[union-attr]
            "stale": stale.is_idle(60),
            "exactly_at_the_threshold": on_the_line.is_idle(60),
        }
        pruned = sorted(store.prune_idle(60))
        remaining = sorted(p.user_id for p in store.get_all())
    finally:
        time.time = real_time  # type: ignore[assignment]
    return {"idle_states": idle_states, "pruned": pruned, "remaining": remaining}


def _record_messages() -> dict[str, Any]:
    """The wire shapes."""
    real_time = time.time
    time.time = lambda: NOW  # type: ignore[assignment]
    try:
        store = _store_with()
        sync = store.get_sync_payload({"idle_threshold_s": 60})
    finally:
        time.time = real_time  # type: ignore[assignment]
    return {
        "types": {
            "presence_update": MSG_PRESENCE_UPDATE,
            "presence_sync": MSG_PRESENCE_SYNC,
            "presence_leave": MSG_PRESENCE_LEAVE,
            "control_transfer": MSG_CONTROL_TRANSFER,
            "queued_input": MSG_QUEUED_INPUT,
            "control_request": MSG_CONTROL_REQUEST,
            "auto_transfer_warning": MSG_AUTO_TRANSFER_WARNING,
        },
        "bare_update": make_presence_update("u1", "Alice", "#ff0000", "operator"),
        "full_update": make_presence_update(
            "u1",
            "Alice",
            "#ff0000",
            "operator",
            scroll_line=10,
            scroll_range=[0, 24],
            total_lines=100,
            selection={"start": 1},
            pin={"row": 2},
            typing=True,
            queued_keys="ls",
            is_owner=True,
        ),
        "update_ignores_unknown_fields": make_presence_update(
            "u1", "Alice", "#ff0000", "operator", nonsense=1, cols=80
        ),
        "leave": make_presence_leave("u1"),
        "transfer": make_control_transfer("u1", "u2", "handover", "ls"),
        "transfer_without_queue": make_control_transfer("u1", "u2", "auto_idle"),
        "sync": sync,
        "empty_sync": make_presence_sync([], {}),
    }


def main() -> int:
    """Write the golden corpus and report the case count."""
    corpus = {
        "key_symbols": dict(KEY_SYMBOLS),
        "keys": [{"name": name, "raw": raw, "display": encode_keys_display(raw)} for name, raw in KEY_CASES],
        "validation": _record_validation(),
        "partial_update": _record_partial_update(),
        "store": _record_store(),
        "idle": _record_idle(),
        "messages": _record_messages(),
        "max_dict_bytes": presence_module._MAX_PRESENCE_DICT_BYTES,
        "max_dict_keys": presence_module._MAX_PRESENCE_DICT_KEYS,
        "validated_fields": sorted(presence_module._VALIDATED_PRESENCE_FIELDS),
        "default_user": UserPresence(user_id="u", name="n", color="c", role="r").to_dict(),
    }
    OUT.write_text(json.dumps(corpus, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(KEY_CASES)} key cases, {len(VALIDATION_CASES)} validation cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
