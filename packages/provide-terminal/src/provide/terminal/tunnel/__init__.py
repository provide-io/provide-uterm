#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tunnel protocol — multiplexed binary channels over WebSocket."""

from provide.terminal.tunnel.protocol import (
    CHANNEL_CONTROL,
    CHANNEL_DATA,
    CHANNEL_HTTP,
    CHANNEL_TCP,
    FLAG_DATA,
    FLAG_EOF,
    TunnelFrame,
    TunnelProtocolError,
    decode_control,
    decode_frame,
    encode_control,
    encode_frame,
)

__all__ = [
    "CHANNEL_CONTROL",
    "CHANNEL_DATA",
    "CHANNEL_HTTP",
    "CHANNEL_TCP",
    "FLAG_DATA",
    "FLAG_EOF",
    "TunnelFrame",
    "TunnelProtocolError",
    "decode_control",
    "decode_frame",
    "encode_control",
    "encode_frame",
]
