#!/usr/bin/env python
# SPDX-FileCopyrightText: Copyright (c) 2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Memray stress test for ControlChannel encoding/decoding."""

from provide.terminal.control_channel import ControlChannelDecoder, encode_control, encode_data

# Payload size variants
SMALL = "x" * 10
MEDIUM = "y" * 200
LARGE = "z" * 2000


def main() -> None:
    """Stress encode/decode cycles with varying payload sizes."""
    decoder = ControlChannelDecoder()

    # Terminal data path: 500K encode_data + decoder.feed cycles
    payloads = [SMALL, MEDIUM, LARGE]
    for _ in range(500_000 // len(payloads)):
        for payload in payloads:
            encoded = encode_data(payload)
            decoder.feed(encoded)

    # Control path: 100K encode_control + decoder.feed cycles
    for _ in range(100_000):
        control_msg = {"type": "snapshot", "data": "x" * 200}
        encoded = encode_control(control_msg)
        decoder.feed(encoded)


if __name__ == "__main__":
    main()
