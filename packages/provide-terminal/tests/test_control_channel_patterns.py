#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for LinkPattern and LinkPatternRegistry (control_channel_patterns)."""

from __future__ import annotations

import asyncio

import pytest

from provide.terminal.control_channel import ControlChannelDecoder, ControlChunk, encode_control
from provide.terminal.control_channel_patterns import LinkPattern, LinkPatternRegistry

# ---------------------------------------------------------------------------
# LinkPattern construction
# ---------------------------------------------------------------------------


class TestLinkPatternConstruction:
    def test_defaults(self) -> None:
        p = LinkPattern(pattern=r"\d+", action="cmd")
        assert p.pattern == r"\d+"
        assert p.action == "cmd"
        assert p.id is None
        assert p.flags == "g"
        assert p.group == 0
        assert p.payload == ""
        assert p.hover == ""
        assert p.class_ == ""

    def test_all_fields(self) -> None:
        p = LinkPattern(
            pattern=r"\((\d{1,5})\)",
            action="key",
            id="sector-nav",
            flags="gi",
            group=1,
            payload="$1\r",
            hover="Warp to sector $1",
            class_="sector",
        )
        assert p.id == "sector-nav"
        assert p.flags == "gi"
        assert p.group == 1
        assert p.payload == "$1\r"
        assert p.hover == "Warp to sector $1"
        assert p.class_ == "sector"

    def test_invalid_action_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid action"):
            LinkPattern(pattern=r"\d+", action="explode")  # type: ignore[arg-type]

    def test_all_valid_actions_accepted(self) -> None:
        for action in ("cmd", "url", "key", "focus"):
            p = LinkPattern(pattern="x", action=action)  # type: ignore[arg-type]
            assert p.action == action

    def test_frozen(self) -> None:
        import dataclasses

        p = LinkPattern(pattern="x", action="cmd")
        with pytest.raises(dataclasses.FrozenInstanceError):
            p.pattern = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LinkPattern.to_frame_entry
# ---------------------------------------------------------------------------


class TestToFrameEntry:
    def test_minimal_entry_contains_required_fields_only(self) -> None:
        p = LinkPattern(pattern=r"\d+", action="url")
        entry = p.to_frame_entry()
        assert entry == {"pattern": r"\d+", "action": "url"}

    def test_id_included_when_set(self) -> None:
        p = LinkPattern(pattern="x", action="cmd", id="my-id")
        assert p.to_frame_entry()["id"] == "my-id"

    def test_flags_omitted_when_default(self) -> None:
        p = LinkPattern(pattern="x", action="cmd", flags="g")
        assert "flags" not in p.to_frame_entry()

    def test_flags_included_when_non_default(self) -> None:
        p = LinkPattern(pattern="x", action="cmd", flags="gi")
        assert p.to_frame_entry()["flags"] == "gi"

    def test_group_omitted_when_zero(self) -> None:
        p = LinkPattern(pattern="x", action="cmd", group=0)
        assert "group" not in p.to_frame_entry()

    def test_group_included_when_nonzero(self) -> None:
        p = LinkPattern(pattern="x", action="cmd", group=2)
        assert p.to_frame_entry()["group"] == 2

    def test_class_key_not_class_underscore(self) -> None:
        p = LinkPattern(pattern="x", action="cmd", class_="sector")
        entry = p.to_frame_entry()
        assert "class" in entry
        assert "class_" not in entry
        assert entry["class"] == "sector"

    def test_empty_class_omitted(self) -> None:
        p = LinkPattern(pattern="x", action="cmd", class_="")
        assert "class" not in p.to_frame_entry()

    def test_full_entry_shape(self) -> None:
        p = LinkPattern(
            pattern=r"\((\d{1,5})\)",
            action="cmd",
            id="sector-nav",
            flags="gi",
            group=1,
            payload="$1\r",
            hover="Warp to sector $1",
            class_="sector",
        )
        entry = p.to_frame_entry()
        assert entry == {
            "pattern": r"\((\d{1,5})\)",
            "action": "cmd",
            "id": "sector-nav",
            "flags": "gi",
            "group": 1,
            "payload": "$1\r",
            "hover": "Warp to sector $1",
            "class": "sector",
        }


# ---------------------------------------------------------------------------
# LinkPatternRegistry
# ---------------------------------------------------------------------------


class TestLinkPatternRegistryBasics:
    def test_empty_sync_payload(self) -> None:
        reg = LinkPatternRegistry()
        assert reg.sync_payload() == {"type": "link_patterns", "patterns": []}

    def test_register_and_get_all(self) -> None:
        reg = LinkPatternRegistry()
        p = LinkPattern(pattern=r"\d+", action="cmd", id="a")
        reg.register(p)
        assert reg.get_all() == [p]

    def test_sync_payload_contains_frame_entries(self) -> None:
        reg = LinkPatternRegistry()
        p = LinkPattern(pattern=r"\d+", action="cmd", id="a")
        reg.register(p)
        payload = reg.sync_payload()
        assert payload["type"] == "link_patterns"
        assert payload["patterns"] == [p.to_frame_entry()]

    def test_insertion_order_preserved(self) -> None:
        reg = LinkPatternRegistry()
        a = LinkPattern(pattern="a", action="cmd", id="a")
        b = LinkPattern(pattern="b", action="url", id="b")
        c = LinkPattern(pattern="c", action="key", id="c")
        for pat in (a, b, c):
            reg.register(pat)
        assert reg.get_all() == [a, b, c]
        patterns = reg.sync_payload()["patterns"]
        assert [e["pattern"] for e in patterns] == ["a", "b", "c"]

    def test_unregister_existing_returns_true(self) -> None:
        reg = LinkPatternRegistry()
        reg.register(LinkPattern(pattern="x", action="cmd", id="x"))
        assert reg.unregister("x") is True
        assert reg.get_all() == []

    def test_unregister_missing_returns_false(self) -> None:
        reg = LinkPatternRegistry()
        assert reg.unregister("nonexistent") is False

    def test_clear_empties_registry(self) -> None:
        reg = LinkPatternRegistry()
        for i in range(3):
            reg.register(LinkPattern(pattern=str(i), action="cmd", id=str(i)))
        reg.clear()
        assert reg.get_all() == []
        assert reg.sync_payload() == {"type": "link_patterns", "patterns": []}

    def test_sync_payload_is_non_destructive(self) -> None:
        reg = LinkPatternRegistry()
        p = LinkPattern(pattern="x", action="cmd", id="x")
        reg.register(p)
        _ = reg.sync_payload()
        assert reg.get_all() == [p]

    def test_sync_payload_returns_new_dict_each_call(self) -> None:
        reg = LinkPatternRegistry()
        reg.register(LinkPattern(pattern="x", action="cmd", id="x"))
        a = reg.sync_payload()
        b = reg.sync_payload()
        assert a == b
        assert a is not b

    def test_pattern_without_id_allowed(self) -> None:
        reg = LinkPatternRegistry()
        p = LinkPattern(pattern="x", action="cmd")  # id=None
        reg.register(p)
        assert reg.get_all() == [p]

    def test_multiple_id_none_patterns_all_kept(self) -> None:
        reg = LinkPatternRegistry()
        p1 = LinkPattern(pattern="a", action="cmd")
        p2 = LinkPattern(pattern="b", action="cmd")
        reg.register(p1)
        reg.register(p2)
        assert reg.get_all() == [p1, p2]

    def test_id_none_patterns_cannot_be_unregistered_individually(self) -> None:
        reg = LinkPatternRegistry()
        reg.register(LinkPattern(pattern="x", action="cmd"))  # id=None
        # Passing a string id that was never registered returns False
        assert reg.unregister("0") is False
        assert len(reg.get_all()) == 1

    def test_same_id_replace_previous(self) -> None:
        """Registering a second pattern with the same id replaces the first."""
        reg = LinkPatternRegistry()
        p1 = LinkPattern(pattern="first", action="cmd", id="shared")
        p2 = LinkPattern(pattern="second", action="url", id="shared")
        reg.register(p1)
        reg.register(p2)
        all_patterns = reg.get_all()
        assert all_patterns == [p2]
        # The replacement preserves the slot (still one entry)
        assert len(all_patterns) == 1

    def test_same_id_replace_preserves_order(self) -> None:
        """The slot position is preserved when replacing by id."""
        reg = LinkPatternRegistry()
        a = LinkPattern(pattern="a", action="cmd", id="a")
        b = LinkPattern(pattern="b", action="cmd", id="b")
        c = LinkPattern(pattern="c", action="cmd", id="c")
        b2 = LinkPattern(pattern="b-updated", action="url", id="b")
        for pat in (a, b, c):
            reg.register(pat)
        reg.register(b2)
        patterns = reg.get_all()
        assert [p.pattern for p in patterns] == ["a", "b-updated", "c"]

    def test_clear_resets_counter_so_id_less_dont_collide(self) -> None:
        reg = LinkPatternRegistry()
        reg.register(LinkPattern(pattern="a", action="cmd"))
        reg.clear()
        reg.register(LinkPattern(pattern="b", action="cmd"))
        assert len(reg.get_all()) == 1


# ---------------------------------------------------------------------------
# Concurrent-safety smoke test
# ---------------------------------------------------------------------------


class TestConcurrentSafety:
    def test_async_gather_mixed_operations_consistent(self) -> None:
        """Smoke test: concurrent coroutines mutating a single registry must not crash."""

        async def _run() -> None:
            reg = LinkPatternRegistry()

            async def adder(i: int) -> None:
                reg.register(LinkPattern(pattern=str(i), action="cmd", id=f"p{i}"))

            async def remover(i: int) -> None:
                reg.unregister(f"p{i}")

            async def syncer() -> dict:  # type: ignore[type-arg]
                return reg.sync_payload()

            # Register 10 patterns concurrently
            await asyncio.gather(*(adder(i) for i in range(10)))

            # Mix removes and syncs concurrently
            tasks = [remover(i) for i in range(0, 10, 2)] + [syncer() for _ in range(5)]
            await asyncio.gather(*tasks)

            # No exception means no crash.  Final state should be consistent.
            final = reg.sync_payload()
            assert final["type"] == "link_patterns"
            assert isinstance(final["patterns"], list)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Round-trip via encode_control / ControlChannelDecoder
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_three_pattern_registry_round_trips(self) -> None:
        reg = LinkPatternRegistry()
        reg.register(LinkPattern(pattern=r"\((\d{1,5})\)", action="cmd", id="sector-nav", group=1, payload="$1\r"))
        reg.register(LinkPattern(pattern=r"https?://\S+", action="url", id="url-open"))
        reg.register(LinkPattern(pattern=r"#(\w+)", action="focus", id="tag-jump", hover="Jump to #$1"))

        payload = reg.sync_payload()
        encoded = encode_control(payload)

        decoder = ControlChannelDecoder()
        chunks = decoder.feed(encoded)

        assert len(chunks) == 1
        chunk = chunks[0]
        assert isinstance(chunk, ControlChunk)
        assert chunk.control["type"] == "link_patterns"
        assert len(chunk.control["patterns"]) == 3

        # Spot-check individual entries
        entries = chunk.control["patterns"]
        assert entries[0]["id"] == "sector-nav"
        assert entries[0]["group"] == 1
        assert entries[1]["action"] == "url"
        assert entries[2]["hover"] == "Jump to #$1"

    def test_empty_registry_round_trips(self) -> None:
        reg = LinkPatternRegistry()
        encoded = encode_control(reg.sync_payload())
        decoder = ControlChannelDecoder()
        chunks = decoder.feed(encoded)
        assert chunks == [ControlChunk({"type": "link_patterns", "patterns": []})]
