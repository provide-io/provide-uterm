#!/usr/bin/env python
"""Memray stress test for ControlChannel encoding/decoding."""

from provide.uterm.control_channel import ControlFrameDecoder, encode_control_frame, encode_terminal_data

# Payload size variants
SMALL = "x" * 10
MEDIUM = "y" * 200
LARGE = "z" * 2000


def main() -> None:
    """Stress encode/decode cycles with varying payload sizes."""
    decoder = ControlFrameDecoder()

    # Terminal data path: 500K encode_terminal_data + decoder.feed cycles
    payloads = [SMALL, MEDIUM, LARGE]
    for _ in range(500_000 // len(payloads)):
        for payload in payloads:
            encoded = encode_terminal_data(payload)
            decoder.feed(encoded)

    # Control path: 100K encode_control_frame + decoder.feed cycles
    for _ in range(100_000):
        control_msg = {"type": "snapshot", "data": "x" * 200}
        encoded = encode_control_frame(control_msg)
        decoder.feed(encoded)


if __name__ == "__main__":
    main()
