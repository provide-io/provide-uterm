# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ControlChannel gateway stress script for memray profiling.

Simulates 1M encode/decode cycles of data and control messages.
Run via: python -m memray run -o gateway_stress.bin scripts/memray_gateway_stress.py
"""

from __future__ import annotations

from provide.terminal.control_channel import ControlChannelDecoder, encode_control, encode_data

NUM_ITERATIONS = 100_000
DATA_PAYLOADS = [
    "hello world\r\n",
    "x" * 80 + "\r\n",
    "\x1b[32mgreen text\x1b[0m\r\n",
    "\x10" * 5 + "escaped DLE\r\n",
]
CONTROL_PAYLOADS = [
    {"type": "snapshot", "screen_hash": "abc123", "prompt_detected": True},
    {"type": "resize", "cols": 80, "rows": 24},
    {"type": "ping", "ts": 1234567890},
]


def run() -> None:
    decoder = ControlChannelDecoder()

    for i in range(NUM_ITERATIONS):
        data_payload = DATA_PAYLOADS[i % len(DATA_PAYLOADS)]
        control_payload = CONTROL_PAYLOADS[i % len(CONTROL_PAYLOADS)]

        encoded_data = encode_data(data_payload)
        encoded_ctrl = encode_control(control_payload)

        combined = encoded_data + encoded_ctrl + encoded_data

        chunks = decoder.feed(combined)
        _ = chunks  # consume


if __name__ == "__main__":
    run()
