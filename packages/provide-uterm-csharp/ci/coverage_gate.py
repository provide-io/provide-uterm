#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Parse coverlet cobertura XML and enforce a line-coverage floor.

Merges *all* coverage.cobertura.xml files under the results directory so batched
``dotnet test --collect`` runs accumulate hits instead of using only the first file.

Excludes pure data tables and documented OS/socket residual packages from the
denominator.
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
    "Pty/NativeUnixPty.cs",  # posix_spawn/openpty/P-Invoke residual
    "Transports/SshTransport.cs",
    "Transports/TelnetTransport.cs",
    "Transports/WebSocketTransport.cs",
    "Gateway/SshWsGateway.cs",  # live FxSsh accept + channel pump residual
    "Vnc/RfbClient.cs",  # live RFB TCP client residual (unit-tested encode + handshake smoke)
    "Embed/TelnetUpstream.cs",  # live TCP telnet residual (IAC+policy unit-tested via ScriptedTelnetUpstream)
)


def _excluded(name: str) -> bool:
    return any(e in name for e in EXCLUDE_SUBSTR)


def _merge_hits(results: Path) -> tuple[dict[tuple[str, str], int], list[Path]]:
    """Return (line_key -> total_hits, source_files).

    line_key = (filename, line_number) so hits from multiple coverlet runs sum.
    """
    files = sorted(results.rglob("coverage.cobertura.xml"))
    hits: dict[tuple[str, str], int] = {}
    for path in files:
        # Local coverlet output only — not untrusted network XML.
        root = ET.parse(path).getroot()  # noqa: S314
        for cls in root.findall(".//class"):
            fname = cls.attrib.get("filename") or ""
            cname = cls.attrib.get("name") or ""
            if _excluded(fname + " " + cname):
                continue
            for line in cls.findall(".//line"):
                num = line.attrib.get("number") or ""
                key = (fname, num)
                h = int(line.attrib.get("hits", "0"))
                hits[key] = hits.get(key, 0) + h
    return hits, files


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: coverage_gate.py <results-dir> <threshold>", file=sys.stderr)
        return 2
    results = Path(sys.argv[1])
    threshold = float(sys.argv[2])
    hits, files = _merge_hits(results)
    if not files:
        print(f"no coverage.cobertura.xml under {results}", file=sys.stderr)
        return 1
    if not hits:
        print("no coverage lines after exclusions", file=sys.stderr)
        return 1
    total = len(hits)
    covered = sum(1 for h in hits.values() if h > 0)
    pct = 100.0 * covered / total if total else 0.0
    print(f"total coverage: {pct:.2f}% ({covered}/{total}) threshold {threshold}%")
    print(f"merged {len(files)} cobertura file(s) under {results}")
    if pct + 1e-9 < threshold:
        print("coverage below threshold", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
