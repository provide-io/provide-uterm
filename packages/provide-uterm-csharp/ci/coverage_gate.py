#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Parse coverlet cobertura XML and enforce a line-coverage floor.

Excludes pure data tables (UnicodeNormTables, UnicodeWidthTables, CharsetTables)
and documented OS/socket residual packages from the denominator.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EXCLUDE_SUBSTR = (
    "UnicodeNormTables",
    "UnicodeWidthTables",
    "CharsetTables",
    # Generated / build artifacts (coverlet may index source generators under obj/).
    "/obj/",
    "RegexGenerator.g.cs",
    # OS/socket-only residual packages (documented, matching Go residual policy for
    # live-socket / PTY / real-SSH paths that unit tests cannot fully exercise).
    "Pty/PtyTransport.cs",
    "Transports/SshTransport.cs",
    "Transports/TelnetTransport.cs",
    "Transports/WebSocketTransport.cs",
)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: coverage_gate.py <results-dir> <threshold>", file=sys.stderr)
        return 2
    results = Path(sys.argv[1])
    threshold = float(sys.argv[2])
    files = list(results.rglob("coverage.cobertura.xml"))
    if not files:
        print(f"no coverage.cobertura.xml under {results}", file=sys.stderr)
        return 1
    # Local coverlet output only — not untrusted network XML.
    tree = ET.parse(files[0])  # noqa: S314
    root = tree.getroot()
    covered = total = 0
    for cls in root.findall(".//class"):
        name = (cls.attrib.get("filename") or "") + " " + (cls.attrib.get("name") or "")
        if any(e in name for e in EXCLUDE_SUBSTR):
            continue
        for line in cls.findall(".//line"):
            total += 1
            if int(line.attrib.get("hits", "0")) > 0:
                covered += 1
    pct = 100.0 * covered / total if total else 0.0
    print(f"total coverage: {pct:.2f}% ({covered}/{total}) threshold {threshold}%")
    print(f"source: {files[0]}")
    if pct + 1e-9 < threshold:
        print("coverage below threshold", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
