#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generic typed-channel negotiation over the inline control channel."""

from __future__ import annotations

import pytest

from provide.uterm.control_channel import encode_control_frame


def test_handle_hello_negotiates_supported_channels_and_ack_fields() -> None:
    from provide.uterm.channels import ChannelHello, NegotiatedChannels

    channels = NegotiatedChannels({"game": 1, "presence": 2}, default_channel="game")

    ack = channels.handle_hello(
        ChannelHello(channels={"game": 99, "presence": 1, "chat": 5}),
        ack_fields={"intents": ["move"], "adapter_version": "app-v1"},
    )

    assert ack == {
        "type": "hello_ack",
        "channels": {"game": 1, "presence": 1},
        "intents": ["move"],
        "adapter_version": "app-v1",
    }
    assert channels.granted == {"game": 1, "presence": 1}
    assert channels.is_negotiated()
    assert channels.is_negotiated("presence")
    assert not channels.is_negotiated("chat")


def test_handle_hello_rejects_non_positive_bool_and_non_int_versions() -> None:
    from provide.uterm.channels import ChannelHello, NegotiatedChannels

    channels = NegotiatedChannels({"game": 1, "presence": 2}, default_channel="game")

    ack = channels.handle_hello(ChannelHello(channels={"game": 0, "presence": -1, "chat": True, "bad": "2"}))

    assert ack == {"type": "hello_ack", "channels": {}}
    assert channels.granted == {}
    assert not channels.is_negotiated()


def test_ack_fields_cannot_override_reserved_keys() -> None:
    from provide.uterm.channels import ChannelHello, NegotiatedChannels

    channels = NegotiatedChannels({"game": 1})

    with pytest.raises(ValueError, match="reserved hello_ack field"):
        channels.handle_hello(ChannelHello(channels={"game": 1}), ack_fields={"type": "not_ack"})

    with pytest.raises(ValueError, match="reserved hello_ack field"):
        channels.handle_hello(ChannelHello(channels={"game": 1}), ack_fields={"channels": {}})


def test_sequence_survives_live_rehello_but_resets_on_restore() -> None:
    from provide.uterm.channels import ChannelHello, NegotiatedChannels

    channels = NegotiatedChannels({"game": 1}, default_channel="game")
    channels.handle_hello(ChannelHello(channels={"game": 1}))

    assert channels.next_seq() == 1
    assert channels.next_seq() == 2

    channels.handle_hello(ChannelHello(channels={"game": 1}))
    assert channels.next_seq() == 3

    channels.restore_grants({"game": 1})
    assert channels.granted == {"game": 1}
    assert channels.next_seq() == 1


def test_parse_channel_hello_accepts_only_framed_hello_payloads() -> None:
    from provide.uterm.channels import ChannelHello, parse_channel_hello

    raw = encode_control_frame({"type": "hello", "channels": {"game": 1}})

    assert parse_channel_hello(raw) == ChannelHello(channels={"game": 1})
    assert parse_channel_hello("plain terminal text") is None
    assert parse_channel_hello(encode_control_frame({"type": "intent", "channels": {"game": 1}})) is None
    assert parse_channel_hello(encode_control_frame({"type": "hello", "channels": []})) is None
    assert parse_channel_hello("\x10\x02zzzzzzzz:{}") is None


def test_parse_channel_hello_rejects_invalid_channel_entries() -> None:
    from provide.uterm.channels import parse_channel_hello

    assert parse_channel_hello(encode_control_frame({"type": "hello", "channels": {"": 1}})) is None
    assert parse_channel_hello(encode_control_frame({"type": "hello", "channels": {"game": True}})) is None
    assert parse_channel_hello(encode_control_frame({"type": "hello", "channels": {"game": "1"}})) is None
