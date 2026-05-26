#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Coverage tests for control_channel.py missing lines."""

from __future__ import annotations

import pytest

from provide.uterm.control_channel import (
    DLE,
    STX,
    ControlChannelDecoder,
    ControlChannelProtocolError,
    ControlChunk,
    DataChunk,
    encode_control,
)


class TestDataChunkKindProperty:
    """Cover DataChunk.kind property (line 36)."""

    def test_kind_returns_data(self) -> None:
        """Covers line 36: DataChunk.kind returns 'data'."""
        chunk = DataChunk("hello")
        assert chunk.kind == "data"


class TestControlChunkKindProperty:
    """Cover ControlChunk.kind property (line 47)."""

    def test_kind_returns_control(self) -> None:
        """Covers line 47: ControlChunk.kind returns 'control'."""
        chunk = ControlChunk({"type": "ping"})
        assert chunk.kind == "control"


class TestFeedTypeError:
    """Cover feed() TypeError for non-str input (line 74)."""

    def test_feed_raises_for_non_str(self) -> None:
        """Covers line 74: TypeError raised when chunk is not str."""
        decoder = ControlChannelDecoder()
        with pytest.raises(TypeError, match="control channel chunks must be str"):
            decoder.feed(b"binary data")  # type: ignore[arg-type]

    def test_feed_raises_for_int(self) -> None:
        """Covers line 74: TypeError raised for int input."""
        decoder = ControlChannelDecoder()
        with pytest.raises(TypeError, match="control channel chunks must be str"):
            decoder.feed(123)  # type: ignore[arg-type]


class TestFinishWithRemainingBuffer:
    """Cover finish() raising error when buffer is non-empty after drain (line 82)."""

    def test_finish_raises_when_buffer_manually_set_after_drain(self) -> None:
        """Covers line 82: finish() raises if _buffer is non-empty after drain.

        We monkey-patch _drain to return without clearing the buffer,
        simulating residual data that _drain left behind.
        """
        from unittest.mock import patch

        decoder = ControlChannelDecoder()
        # Set buffer to non-empty value
        decoder._buffer = "leftover"

        # Patch _drain so it returns normally without clearing the buffer
        def fake_drain(*, final: bool) -> list:
            return []

        with (
            patch.object(decoder, "_drain", side_effect=fake_drain),
            pytest.raises(ControlChannelProtocolError, match="truncated control frame"),
        ):
            decoder.finish()


class TestDrainFinalDleAtEnd:
    """Cover _drain(final=True) when DLE is at end of buffer (lines 98-100)."""

    def test_final_true_raises_on_trailing_dle(self) -> None:
        """Covers lines 98-100: DLE at end with final=True raises truncated error."""
        decoder = ControlChannelDecoder()
        # DLE followed by nothing — idx+1 >= len means we need final check
        # In final=True mode, this should raise
        with pytest.raises(ControlChannelProtocolError, match="truncated control frame"):
            decoder.feed(DLE)
            decoder.finish()

    def test_feed_partial_dle_buffers_without_error(self) -> None:
        """Covers line 100: DLE at end with final=False just breaks (buffered)."""
        decoder = ControlChannelDecoder()
        # DLE alone in a feed (not final) — should NOT raise, should buffer
        result = decoder.feed(DLE)
        # No events emitted, DLE is buffered
        assert result == []

    def test_finish_raises_on_isolated_dle_in_buffer(self) -> None:
        """Covers lines 98-100: finish() with only DLE in buffer raises."""
        decoder = ControlChannelDecoder()
        decoder.feed(DLE)  # buffered
        with pytest.raises(ControlChannelProtocolError, match="truncated control frame"):
            decoder.finish()


class TestDrainFinalIncompleteHeader:
    """Cover _drain(final=True) when header is incomplete (lines 115-117)."""

    def test_final_true_raises_on_incomplete_header(self) -> None:
        """Covers lines 115-117: DLE+STX present but header incomplete with final=True."""
        decoder = ControlChannelDecoder()
        # Feed DLE+STX plus only a few header bytes (less than 11 total)
        partial = f"{DLE}{STX}0000"  # only 4 hex digits, need 8 + ':'
        decoder.feed(partial)
        with pytest.raises(ControlChannelProtocolError, match="truncated control frame"):
            decoder.finish()

    def test_feed_partial_header_buffers_without_error(self) -> None:
        """Covers line 117: incomplete header with final=False just buffers."""
        decoder = ControlChannelDecoder()
        partial = f"{DLE}{STX}0000"
        result = decoder.feed(partial)
        assert result == []


class TestDrainInvalidJson:
    """Cover _drain JSON decode error path (lines 137-138)."""

    def test_decoder_raises_on_invalid_json_payload(self) -> None:
        """Covers lines 137-138: json.JSONDecodeError wraps as ControlChannelProtocolError."""
        decoder = ControlChannelDecoder()
        # Manually construct a frame with invalid JSON
        bad_payload = "not-json!"
        length_hex = f"{len(bad_payload):08x}"
        raw = f"{DLE}{STX}{length_hex}:{bad_payload}"
        with pytest.raises(ControlChannelProtocolError, match="invalid control json"):
            decoder.feed(raw)


class TestDecoderEdgeCases:
    """Additional edge cases for _drain paths."""

    def test_data_before_control_then_more_data(self) -> None:
        """Covers data_parts flush when control frame starts (line 110-112)."""
        decoder = ControlChannelDecoder()
        raw = "before" + encode_control({"type": "ping"}) + "after"
        events = decoder.feed(raw)
        assert events[0] == DataChunk("before")
        assert events[1] == ControlChunk({"type": "ping"})
        assert events[2] == DataChunk("after")

    def test_finish_empty_buffer_returns_no_events(self) -> None:
        """Covers finish() with no buffered data — no error, no events."""
        decoder = ControlChannelDecoder()
        result = decoder.finish()
        assert result == []

    def test_buffer_overflow_raises_and_clears(self) -> None:
        """Buffer overflow protection (lines 83-88) clears state and raises."""
        from provide.uterm.control_channel import ControlChannelProtocolError

        decoder = ControlChannelDecoder(max_buffer_bytes=5)
        # First feed is within limit (3 bytes ≤ 5)
        decoder.feed("abc")
        # After drain, buffer_parts is empty (data was emitted).
        # Second feed of 6 bytes exceeds max_buffer_bytes=5.
        with pytest.raises(ControlChannelProtocolError, match="buffer overflow"):
            decoder.feed("x" * 6)
        # After overflow, buffer is cleared — next feed starts fresh
        events = decoder.feed("ok")
        assert any(hasattr(e, "data") for e in events) or events == []


class TestCheckJsonDepthListBranch:
    """Cover branches in ``_check_json_depth``.

    The walker has two relevant branches off the dict/list dispatch:
    - ``isinstance(node, list)`` taken → for loop runs (115 true edge).
    - ``isinstance(node, list)`` not taken → fall through to next iteration
      of ``while stack:`` at line 107 (the 115->107 branch).
    """

    def test_list_of_primitives_does_not_extend_stack(self) -> None:
        """A list containing only primitive leaves enters the list branch
        but adds nothing to the depth stack — exercising the list arm."""
        from provide.uterm.control_channel import _check_json_depth

        # Top-level dict (depth 1) holds a list of primitives.
        # When the list is popped at depth 2, the list branch runs but
        # no child is a dict/list, so the for loop exits without pushing
        # anything onto the stack, and control returns to ``while stack:``.
        _check_json_depth({"items": [1, "two", 3.0, True, None]}, max_depth=8)

    def test_primitive_leaf_falls_through_dict_and_list_branches(self) -> None:
        """A non-dict, non-list value at the top of the stack must skip
        both the dict and list branches, returning to the while loop —
        covering the 115->107 fall-through branch.
        """
        from provide.uterm.control_channel import _check_json_depth

        # A bare string is neither dict nor list — neither branch runs and
        # the while loop iterates once before the stack drains.
        _check_json_depth("just-a-leaf", max_depth=4)
        _check_json_depth(42, max_depth=4)


class TestDepthErrorWithOnErrorCallback:
    """Cover line 192: ``self._on_error(...)`` fires when depth check fails
    AND an ``on_error`` callback was provided."""

    def test_depth_violation_invokes_on_error_callback(self) -> None:
        """Covers line 192: on_error is invoked from _parse_frame_payload
        when _check_json_depth raises."""
        # Re-import locally — other tests in this file reload the module,
        # which can detach the top-level ControlChannelProtocolError binding
        # from the runtime class used by the decoder.
        from provide.uterm import control_channel as cc

        errors: list[str] = []
        decoder = cc.ControlChannelDecoder(max_frame_depth=2, on_error=errors.append)
        # depth-3 payload: {"a": {"b": {"c": "leaf"}}}
        payload: dict[str, object] = {"c": "leaf"}
        for k in ("b", "a"):
            payload = {k: payload}
        with pytest.raises(cc.ControlChannelProtocolError, match="nests deeper than 2"):
            decoder.feed(cc.encode_control(payload))
        # on_error fires twice: once inside the depth try-block (line 192),
        # once via _report_error wrapping the protocol error.
        assert errors.count("control_channel_protocol_error") >= 1


class TestOptionalJsonImplementations:
    """Cover the orjson/ujson optional-dep branches in control_channel.

    Neither orjson nor ujson is required by provide-uterm, so by default
    the json fallback is exercised. To cover the orjson and ujson
    implementations honestly we reload the module with fake modules
    inserted into ``sys.modules``.
    """

    def test_orjson_branch_reloads_and_roundtrips(self) -> None:
        """Covers lines 22-25: orjson _json_dumps body and _json_loads bind."""
        import importlib
        import sys
        import types

        fake = types.ModuleType("orjson")

        def fake_dumps(obj: object) -> bytes:
            import json as _json

            return _json.dumps(obj).encode("utf-8")

        def fake_loads(s: str | bytes) -> object:
            import json as _json

            if isinstance(s, (bytes, bytearray)):
                s = s.decode("utf-8")
            return _json.loads(s)

        fake.dumps = fake_dumps  # type: ignore[attr-defined]
        fake.loads = fake_loads  # type: ignore[attr-defined]

        original_orjson = sys.modules.get("orjson")
        original_module = sys.modules["provide.uterm.control_channel"]
        # importlib.reload() mutates the module *in place*, replacing all of
        # its attributes (classes included). Other test files captured the
        # ORIGINAL ``ControlChannelProtocolError`` class via top-level
        # ``from ... import`` and will fail ``pytest.raises(...)`` against a
        # post-reload exception that is a different class. Snapshot every
        # attribute now so the finally-clause can put them back atomically.
        original_attrs = dict(original_module.__dict__)
        sys.modules["orjson"] = fake
        try:
            cc = importlib.reload(original_module)
            # _json_dumps now goes through the orjson branch (line 23).
            out = cc.encode_control({"type": "hello"})
            # _json_loads is orjson.loads (line 25).
            decoder = cc.ControlChannelDecoder()
            events = decoder.feed(out)
            assert events == [cc.ControlChunk({"type": "hello"})]
        finally:
            if original_orjson is None:
                sys.modules.pop("orjson", None)
            else:
                sys.modules["orjson"] = original_orjson
            # Restore every original attribute on the module object in place
            # so other test files' captured class bindings remain valid.
            for key in list(original_module.__dict__):
                if key not in original_attrs:
                    delattr(original_module, key)
            for key, value in original_attrs.items():
                setattr(original_module, key, value)

    def test_ujson_branch_reloads_and_roundtrips(self) -> None:
        """Covers lines 30-33: ujson _json_dumps body and _json_loads bind.

        We block orjson and provide a fake ujson, forcing the
        ``except ImportError`` → inner ``try import ujson`` path.
        """
        import builtins
        import importlib
        import sys
        import types

        fake_ujson = types.ModuleType("ujson")

        def fake_dumps(obj: object, ensure_ascii: bool = True) -> str:
            import json as _json

            return _json.dumps(obj, ensure_ascii=ensure_ascii)

        def fake_loads(s: str | bytes) -> object:
            import json as _json

            if isinstance(s, (bytes, bytearray)):
                s = s.decode("utf-8")
            return _json.loads(s)

        fake_ujson.dumps = fake_dumps  # type: ignore[attr-defined]
        fake_ujson.loads = fake_loads  # type: ignore[attr-defined]

        original_orjson = sys.modules.get("orjson")
        original_ujson = sys.modules.get("ujson")
        original_module = sys.modules["provide.uterm.control_channel"]
        # See note in test_orjson_branch_reloads_and_roundtrips — snapshot every
        # module attribute so the finally-clause can put them back atomically.
        original_attrs = dict(original_module.__dict__)
        # Ensure orjson is unimportable so the ujson branch runs.
        sys.modules["ujson"] = fake_ujson
        sys.modules.pop("orjson", None)
        real_import = builtins.__import__

        def blocked_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "orjson":
                raise ImportError("blocked for test")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        builtins.__import__ = blocked_import  # type: ignore[assignment]
        try:
            cc = importlib.reload(original_module)
            out = cc.encode_control({"type": "hello"})
            decoder = cc.ControlChannelDecoder()
            events = decoder.feed(out)
            assert events == [cc.ControlChunk({"type": "hello"})]
        finally:
            builtins.__import__ = real_import  # type: ignore[assignment]
            if original_orjson is None:
                sys.modules.pop("orjson", None)
            else:
                sys.modules["orjson"] = original_orjson
            if original_ujson is None:
                sys.modules.pop("ujson", None)
            else:
                sys.modules["ujson"] = original_ujson
            # Restore every original attribute on the module object in place
            # so other test files' captured class bindings remain valid.
            for key in list(original_module.__dict__):
                if key not in original_attrs:
                    delattr(original_module, key)
            for key, value in original_attrs.items():
                setattr(original_module, key, value)
