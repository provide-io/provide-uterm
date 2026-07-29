#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Make the live harness importable.

The harness lives beside the scenarios it runs (``conformance/live/harness``)
rather than under a language's package, because it belongs to no one language.

It is imported as ``harness`` from ``conformance/live`` rather than as
``conformance.live.harness`` from the repo root: under pytest the name
``conformance`` already resolves to ``tests/conformance``, which is a package,
and a shadowed import would fail in the suite while working everywhere else.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIVE = Path(__file__).resolve().parents[3] / "conformance" / "live"
if str(_LIVE) not in sys.path:
    sys.path.insert(0, str(_LIVE))
