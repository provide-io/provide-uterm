#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
import sys
import timeit
from pathlib import Path

# Add packages to sys.path
sys.path.insert(0, str(Path("packages/provide-uterm-server/src").resolve()))

from provide.uterm.bridge.hub.ext import RedactionRule
from provide.uterm.bridge.hub.redaction import StreamRedactor


def benchmark():
    rules = [
        RedactionRule(pattern=r"sk_live_[0-9a-zA-Z]+", replacement="[STRIPE_SECRET]"),
        RedactionRule(pattern=r"(\d{4}-){3}\d{4}", replacement="[CREDIT_CARD]"),
        RedactionRule(pattern=r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", replacement="[EMAIL]"),
        RedactionRule(pattern=r"AIza[0-9A-Za-z-_]{35}", replacement="[GOOGLE_API_KEY]"),
        RedactionRule(pattern=r"eyJh[0-9a-zA-Z_-]+\.eyJh[0-9a-zA-Z_-]+\.[0-9a-zA-Z_-]+", replacement="[JWT]"),
    ]

    redactor = StreamRedactor(rules)

    data = (
        "Here is some sensitive data: sk_live_ABC123456789, a card 1234-5678-9012-3456, an email test@example.com, a key AIzaSyA12345678901234567890123456789012, and a JWT eyJhYmMi.eyJhYmMi.YWJj. "
        * 100
    )

    print(f"Input data size: {len(data)} bytes")
    print(f"Number of rules: {len(rules)}")

    number = 1000
    timer = timeit.Timer(lambda: redactor.redact(data))

    duration = timer.timeit(number=number)
    print(f"Redaction took {duration:.4f} seconds for {number} iterations")
    print(f"Average time per redaction: {duration / number * 1000:.4f} ms")


if __name__ == "__main__":
    benchmark()
