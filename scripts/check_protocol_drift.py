#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Fail when the wire contract has drifted between the ports.

Three independent drifts, none of which any existing check could see:

1. **Protocol-version disagreement.** The negotiated ``min``/``max``/
   ``preferred`` triple is declared in *six* places. Python and TypeScript keep
   one declaration each, but Go and C# each declare it twice — once for the
   bridge and once for the shell's ``worker_hello`` — with nothing tying the two
   together. A port that bumped ``max`` in its bridge and not its shell would
   advertise one range and negotiate another, and every test in that port would
   still pass, because each side is self-consistent.

2. **Divergent copies of a shared fixture.** ``signature_corpus.json`` is
   committed twice, byte-identical, under the Go and C# test trees. Regenerating
   one leaves the other asserting the old behaviour. ``ci/check_fuzz_corpus.sh``
   covers the two ``conformance/fuzz`` corpora and does not reach these.

3. **A protocol change that never reached the docs.** With ``--changed-against``,
   a diff that touches a wire-contract source without touching the protocol
   matrix fails. This is the "CI fails on stale matrix/docs when protocol
   changes" half of the guardrail; the first two run unconditionally.

Usage:
    uv run python scripts/check_protocol_drift.py
    uv run python scripts/check_protocol_drift.py --changed-against origin/main
"""

from __future__ import annotations

import argparse
import re
import subprocess
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (label, path, {bound: pattern}). Each pattern must capture exactly one integer.
# Kept as explicit per-language patterns rather than one loose regex so that a
# renamed or deleted constant fails loudly instead of silently matching nothing.
VERSION_SITES: tuple[tuple[str, str, dict[str, str]], ...] = (
    (
        "python/bridge.contracts",
        "packages/provide-uterm/src/provide/uterm/bridge/contracts.py",
        {
            "min": r"^MIN_PROTOCOL_VERSION\s*=\s*(\d+)",
            "max": r"^MAX_PROTOCOL_VERSION\s*=\s*(\d+)",
            "preferred": r"^PREFERRED_PROTOCOL_VERSION\s*=\s*(\d+)",
        },
    ),
    (
        "go/bridge.contracts",
        "packages/provide-uterm-go/bridge/contracts.go",
        {
            "min": r"\bMinProtocolVersion\s*=\s*(\d+)",
            "max": r"\bMaxProtocolVersion\s*=\s*(\d+)",
            "preferred": r"\bPreferredProtocolVersion\s*=\s*(\d+)",
        },
    ),
    (
        "go/shell.frame",
        "packages/provide-uterm-go/shell/frame.go",
        {
            "min": r"\bminProtocolVersion\s*=\s*(\d+)",
            "max": r"\bmaxProtocolVersion\s*=\s*(\d+)",
            "preferred": r"\bpreferredProtocolVersion\s*=\s*(\d+)",
        },
    ),
    (
        "csharp/Bridge.Contracts",
        "packages/provide-uterm-csharp/src/Provide.Uterm/Bridge/Contracts.cs",
        {
            "min": r"\bMinProtocolVersion\s*=\s*(\d+)\s*;",
            "max": r"\bMaxProtocolVersion\s*=\s*(\d+)\s*;",
            "preferred": r"\bPreferredProtocolVersion\s*=\s*(\d+)\s*;",
        },
    ),
    (
        "csharp/Shell.Frame",
        "packages/provide-uterm-csharp/src/Provide.Uterm/Shell/Frame.cs",
        {
            "min": r"\bMinProtocolVersion\s*=\s*(\d+)\s*;",
            "max": r"\bMaxProtocolVersion\s*=\s*(\d+)\s*;",
            "preferred": r"\bPreferredProtocolVersion\s*=\s*(\d+)\s*;",
        },
    ),
    (
        "typescript/bridge.contracts",
        "packages/provide-uterm-ts/src/bridge/contracts.ts",
        {
            "min": r"\bMIN_PROTOCOL_VERSION\s*=\s*(\d+)",
            "max": r"\bMAX_PROTOCOL_VERSION\s*=\s*(\d+)",
            "preferred": r"\bPREFERRED_PROTOCOL_VERSION\s*=\s*(\d+)",
        },
    ),
)

# Fixtures committed in more than one place that must stay byte-identical.
TWINNED_FIXTURES: tuple[tuple[str, ...], ...] = (
    (
        "packages/provide-uterm-go/ctrlmsg/testdata/signature_corpus.json",
        "packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/testdata/ctrlmsg/signature_corpus.json",
    ),
)

# Touching any of these changes what goes on the wire.
PROTOCOL_SOURCES: tuple[str, ...] = (
    "spec/behavior.json",
    "spec/behavior_vectors.json",
    "spec/uterm-api.yaml",
    "spec/session_lifecycle_security_scenarios.json",
    "spec/fanout_security_scenarios.json",
    "packages/provide-uterm/src/provide/uterm/bridge/schemas.py",
    *(path for _, path, _ in VERSION_SITES),
)

# A protocol source may not move without the matrix that documents it moving too.
PROTOCOL_DOCS: tuple[str, ...] = ("docs/protocol-matrix.md",)


def check_versions() -> list[str]:
    """Require one agreed min/max/preferred triple across every port."""
    errors: list[str] = []
    observed: dict[str, dict[str, int]] = {}
    for label, relative, patterns in VERSION_SITES:
        path = ROOT / relative
        if not path.is_file():
            errors.append(f"{label}: missing declaration file {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        bounds: dict[str, int] = {}
        for bound, pattern in patterns.items():
            match = re.search(pattern, text, re.MULTILINE)
            if match is None:
                errors.append(f"{label}: no {bound} protocol-version declaration in {relative}")
                continue
            bounds[bound] = int(match.group(1))
        if len(bounds) == len(patterns):
            observed[label] = bounds

    if len(observed) < len(VERSION_SITES):
        return errors

    reference_label, reference = next(iter(observed.items()))
    for label, bounds in observed.items():
        if bounds != reference:
            errors.append(f"protocol-version drift: {label} declares {bounds}, {reference_label} declares {reference}")
    return errors


def check_twinned_fixtures() -> list[str]:
    """Require every committed copy of a shared fixture to be byte-identical."""
    errors: list[str] = []
    for group in TWINNED_FIXTURES:
        digests: dict[str, str] = {}
        for relative in group:
            path = ROOT / relative
            if not path.is_file():
                errors.append(f"missing twinned fixture {relative}")
                continue
            digests[relative] = sha256(path.read_bytes()).hexdigest()
        if len(set(digests.values())) > 1:
            detail = ", ".join(f"{name}={digest[:12]}" for name, digest in sorted(digests.items()))
            errors.append(f"twinned fixture drift: {detail}")
    return errors


def _changed_files(ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{ref}...HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git diff against {ref} failed")
    return [line for line in result.stdout.splitlines() if line]


def check_docs_followed(ref: str) -> list[str]:
    """Require a protocol-source change to carry a protocol-doc change."""
    try:
        changed = set(_changed_files(ref))
    except RuntimeError as exc:
        return [f"could not diff against {ref}: {exc}"]

    touched_protocol = sorted(changed.intersection(PROTOCOL_SOURCES))
    if not touched_protocol:
        return []
    if changed.intersection(PROTOCOL_DOCS):
        return []
    return [
        "protocol sources changed without the protocol matrix: "
        + ", ".join(touched_protocol)
        + f" -- update {' or '.join(PROTOCOL_DOCS)} (or state in the PR why the wire contract is unchanged)"
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-against",
        metavar="REF",
        help="also require a protocol-doc update for any protocol-source change since REF",
    )
    args = parser.parse_args(argv)

    errors = [*check_versions(), *check_twinned_fixtures()]
    if args.changed_against:
        errors.extend(check_docs_followed(args.changed_against))

    if errors:
        print("protocol drift check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("protocol drift check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
