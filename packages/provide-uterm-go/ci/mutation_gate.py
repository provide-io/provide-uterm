#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-testing gate for the Go port's well-covered wire/library packages.

Runs ``gremlins unleash`` (github.com/go-gremlins/gremlins) over a small,
deliberately-scoped perimeter and enforces the same bar as the Python
mutation gate: every mutant must be KILLED, except a documented allowlist of
genuinely-equivalent survivors (``mutation_equivalents.toml``).

Why the perimeter is small: mutation testing recompiles + reruns the package
test binary once per covered mutant, so it is far slower than `go test`. We
scope it to a handful of pure-function packages that are already at ~100%
statement coverage and carry real branch/boundary/arithmetic logic — where a
mutant that survives points at a genuine assertion gap — rather than the whole
~50-package module (which would be slow and dominated by integration packages
whose residual lines need live sockets/PTYs to exercise).

Most PERIMETER entries are a bare package name (gremlins mutates every file in
it). Some packages are otherwise too large / I/O-heavy to mutate wholesale but
still contain a handful of pure, well-isolated, security-critical files worth
the same bar — for those, a ScopedPackage narrows gremlins to just those files
via ``--exclude-files`` (confirmed by `gremlins unleash --help` to take a
filepath regexp, repeatable). The exclude set is *computed from the package
directory at gate run time* rather than hand-listed: a file later added to the
package is excluded by default (narrowing), so silently pulling new,
untested code into the mutated scope requires deliberately adding it to
`only_files` here — the same "widen on purpose" discipline as adding a new
bare-package entry.

Gate rules (mirroring scripts/run_mutation_gate.py):
  * NOT_COVERED mutant  -> FAIL (coverage gap; the perimeter is 100% covered).
  * TIMED_OUT mutant    -> FAIL. A timeout can mask a real survivor (a
    fast-passing mutant test cut off below `go test` compile time), so we treat
    any timeout as a failure and raise --timeout-coefficient until it clears.
  * LIVED mutant        -> FAIL unless listed in mutation_equivalents.toml.
  * Stale allowlist entry (never matched a survivor) -> WARN (non-fatal).

Stdlib only (tomllib needs Python >= 3.11), so it runs from a bare Go checkout
with just `python3` on PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScopedPackage:
    """A perimeter entry that mutates only `only_files` inside `package`.

    Use this instead of a bare package-name string when the rest of the
    package is integration code (HTTP routing, WS/PTY handling, live sockets)
    that does not belong in the mutation perimeter. See the module docstring
    for why the exclude set is computed rather than hand-listed.
    """

    package: str
    only_files: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.package}[{'+'.join(self.only_files)}]"


# Perimeter: pure-function packages already at ~100% statement coverage with
# real boundary/arithmetic logic. Keep this list short so a full run stays a
# couple of minutes. Extend deliberately, one well-covered package (or scoped
# file set) at a time.
PERIMETER: tuple[str | ScopedPackage, ...] = (
    "sanitizer",
    "colors",
    "filters",
    "lineeditor",
    "redaction",
    "channels",
    "frames",
    "policy",  # StrictPolicyEngine — cross-language behavior contract
    "defaults",  # TerminalDefaults pure constants/helpers at 100% cover
    "fileio",  # SecureOpenAppend modes + palette/ans pure helpers
    # `server` is ~9k LOC of HTTP routing / WS / PTY-adjacent integration code
    # needing live sockets — not perimeter material wholesale — but these three
    # files are the SSRF egress classifier + the webhook manager (registry,
    # delivery loop, retry ladder, HMAC signing, tunnel-share guard): pure
    # classification/business logic, already unit-tested with injected
    # resolver/clock/HTTP-client seams, no real network needed.
    ScopedPackage("server", ("server_egress.go", "server_egress_webhook.go", "server_webhooks.go")),
    # `serverconfig` also has file-based config loading (load.go) and a file-
    # backed profile store (profiles.go) that don't belong here; validate.go
    # (incl. IsLoopbackHost, the §3 webhook-loopback permission source) is pure
    # validation logic and already ~100% unit-tested.
    ScopedPackage("serverconfig", ("validate.go",)),
    # `cli` is the CLI + the in-memory session registry behind it. Only
    # registry.go is perimeter material: it owns the session lifecycle
    # (create/update/delete, connector stop, worker-bridge detach) that the
    # Python SessionRegistry is mutation-enforced for, so the two ports are
    # held to the same bar. Widened on purpose on 2026-08-06, after a
    # DeleteSession that stopped the connector but leaked the hub worker
    # bridge shipped with only one test pinning the fix.
    ScopedPackage("cli", ("registry.go",)),
)

# Pinned like golangci-lint/govulncheck in the Makefile: invoked via `go run`
# so the version is reproducible without a go.mod tool dependency.
GREMLINS = "github.com/go-gremlins/gremlins/cmd/gremlins@v0.6.0"

# gremlins derives each mutant's test timeout from the (tiny) baseline test
# time; a low coefficient spuriously times out mutants whose recompile exceeds
# the budget — which can hide real survivors. 100 gives every perimeter package
# zero timeouts locally. The gate fails on any timeout, so a too-low value on a
# slow runner surfaces loudly rather than silently.
DEFAULT_COEFFICIENT = 100

# Wall-clock budgets. gremlins derives each mutant's timeout from the baseline
# test time, so a slow runner scales every mutant's leash at once — the gate
# does not get slower in one visible place, it gets slower everywhere. On
# 2026-08-03 that took a job whose four previous runs took five minutes to
# past twenty, where GitHub killed it; because each package's line is printed
# only after that package finishes, the run died having said nothing at all
# about where it was.
#
# These exist so the gate loses that race deliberately: it fails first, naming
# the package it was in. The job's own timeout-minutes stays the backstop, not
# the mechanism.
DEFAULT_PACKAGE_TIMEOUT_S = 300
DEFAULT_BUDGET_S = 900

MODULE_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_FILE = MODULE_ROOT / "mutation_equivalents.toml"

# gremlins statuses that are never a gate failure. KILLED = caught; NOT_VIABLE =
# the mutation did not compile; SKIPPED = not analysed.
OK_STATUSES = frozenset({"KILLED", "NOT_VIABLE", "SKIPPED"})


# Allowlist key: CONTENT, not coordinates — the same change the C# and TS gates
# took on 2026-08-12, for the same reason. A line number is a property of
# everything ABOVE a mutant, so an unrelated insertion moved every entry below
# it; and because this file is a mutation support file, renumbering it is itself
# a change that widens the next run. See ci/stryker_gate.py in the C# port for
# the incident.
#
#   package/file  where it lives
#   mutator       gremlins' mutation type
#   code          the source line, stripped — also what makes an entry reviewable
#   occurrence    1-based among identical mutants on that line (omitted when 1)
#
# gremlins reports no replacement text, so unlike the Stryker gates the key
# cannot include one; `code` plus `occurrence` carries the discrimination
# instead. Two CONDITIONALS_BOUNDARY mutants on one `if a > 0 && b > 0` differ
# only by occurrence.
AllowKey = tuple[str, str, str, str, int]


def source_line(package: str, file_name: str, line: int) -> str:
    """The stripped source line a mutant sits on, read from the working tree."""
    path = MODULE_ROOT / package / file_name
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[line - 1].strip() if 0 <= line - 1 < len(lines) else ""


def file_keys(package: str, file_entry: dict) -> dict[int, AllowKey]:
    """Map each mutation's index in one report file entry to its content key.

    `occurrence` counts over EVERY mutation reported for the file, not just the
    surviving ones, so a mutant that starts or stops being killed cannot
    renumber its neighbours.
    """
    file_name = file_entry["file_name"]
    mutations = file_entry.get("mutations", [])
    order = sorted(
        range(len(mutations)),
        key=lambda i: (int(mutations[i]["line"]), int(mutations[i]["column"]), mutations[i]["type"]),
    )
    counts: dict[tuple[str, str], int] = {}
    out: dict[int, AllowKey] = {}
    for index in order:
        mutation = mutations[index]
        code = source_line(package, file_name, int(mutation["line"]))
        shape = (mutation["type"], code)
        counts[shape] = counts.get(shape, 0) + 1
        out[index] = (package, file_name, mutation["type"], code, counts[shape])
    return out


def load_allowlist() -> dict[AllowKey, str]:
    """Return {(package, file, mutator, code, occurrence): reason} from the TOML."""
    if not ALLOWLIST_FILE.exists():
        return {}
    data = tomllib.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))
    out: dict[AllowKey, str] = {}
    for entry in data.get("equivalent", []):
        key: AllowKey = (
            entry["package"],
            entry["file"],
            entry["mutator"],
            entry["code"],
            int(entry.get("occurrence", 1)),
        )
        out[key] = entry["reason"]
    return out


def describe(key: AllowKey) -> str:
    """One-line rendering of a key, for stale-entry warnings."""
    package, file_name, mutator, code, occurrence = key
    nth = f" #{occurrence}" if occurrence > 1 else ""
    return f"{package}/{file_name} {mutator}{nth} in {code!r}"


def exclude_file_args(package: str, only_files: tuple[str, ...]) -> list[str]:
    """Build `-E <regexp>` args excluding every source file in `package` NOT in
    `only_files`, scanned from disk right now (see ScopedPackage docstring for
    why this is computed rather than hand-listed). Test files are skipped on
    both sides: gremlins never mutates them, so excluding them is a no-op.
    """
    args: list[str] = []
    for path in sorted((MODULE_ROOT / package).glob("*.go")):
        if path.name.endswith("_test.go") or path.name in only_files:
            continue
        args += ["-E", re.escape(path.name) + "$"]
    return args


def _decoded(stream: bytes | str | None) -> str:
    """Captured output as text, whatever form the kill left it in."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def remaining_budget(budget_s: float, started_at: float, now: float) -> float:
    """Seconds left of the overall budget, never negative."""
    return max(0.0, budget_s - (now - started_at))


def package_timeout(package_timeout_s: float, budget_left: float) -> float:
    """How long one package may take: its own cap, or what is left of the budget.

    Whichever is smaller. A package allowed to run past the overall budget would
    hand the kill back to the job's timeout, which is the silent failure this
    replaces.
    """
    return min(package_timeout_s, budget_left)


class GateTimeoutError(RuntimeError):
    """A package, or the whole gate, ran past its wall-clock budget."""


def run_gremlins(
    package: str,
    coefficient: int,
    only_files: tuple[str, ...] = (),
    timeout_s: float = DEFAULT_PACKAGE_TIMEOUT_S,
) -> dict:
    """Run `gremlins unleash` on one package and return its parsed JSON report.

    only_files, when non-empty, narrows mutation to just those files via
    --exclude-files (everything else in the package directory is excluded).

    timeout_s bounds the run. Without it a package that stops making progress
    takes the whole job down with it and reports nothing, because gremlins'
    output is captured and only surfaces once the run returns.
    """
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = [
            "go",
            "run",
            GREMLINS,
            "unleash",
            "--timeout-coefficient",
            str(coefficient),
            *(exclude_file_args(package, only_files) if only_files else []),
            "-o",
            str(out_path),
            f"./{package}/",
        ]
        try:
            proc = subprocess.run(  # nosec
                cmd, cwd=MODULE_ROOT, capture_output=True, text=True, check=False, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as expired:
            # Surface whatever gremlins managed to say. Captured output is lost
            # on a kill otherwise, and "it hung" without a package name is the
            # report that started this.
            sys.stdout.write(_decoded(expired.stdout))
            sys.stderr.write(_decoded(expired.stderr))
            msg = (
                f"gremlins exceeded {timeout_s:.0f}s on {package!r}. "
                f"Either the runner is slow enough that --timeout-coefficient "
                f"{coefficient} scales every mutant past the budget, or this "
                f"package stopped making progress."
            )
            raise GateTimeoutError(msg) from expired
        text = out_path.read_text(encoding="utf-8").strip()
        if not text:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            msg = f"gremlins produced no JSON for {package} (exit {proc.returncode})"
            raise RuntimeError(msg)
        return json.loads(text)
    finally:
        out_path.unlink(missing_ok=True)


def check_package(
    package: str,
    report: dict,
    allowlist: dict[tuple[str, str, int, int, str], str],
    used: set[tuple[str, str, int, int, str]],
) -> tuple[int, int, list[str]]:
    """Return (killed, survivors_ok, failures) for one package report."""
    killed = 0
    excused = 0
    failures: list[str] = []
    for file_entry in report.get("files", []):
        fname = file_entry["file_name"]
        keys_by_index = file_keys(package, file_entry)
        for index, mut in enumerate(file_entry.get("mutations", [])):
            status = mut["status"]
            if status == "KILLED":
                killed += 1
                continue
            if status in OK_STATUSES:
                continue
            key = keys_by_index[index]
            # Coordinates remain the most useful thing to PRINT — they are just
            # no longer what an entry is matched on.
            loc = f"{package}/{fname}:{mut['line']}:{mut['column']} {mut['type']} [{status}]"
            if status == "LIVED" and key in allowlist:
                used.add(key)
                excused += 1
                continue
            failures.append(loc)
    return killed, excused, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout-coefficient",
        type=int,
        default=DEFAULT_COEFFICIENT,
        help=f"gremlins per-mutant timeout coefficient (default {DEFAULT_COEFFICIENT})",
    )
    parser.add_argument(
        "--package-timeout",
        type=float,
        default=DEFAULT_PACKAGE_TIMEOUT_S,
        help=f"seconds one package may take (default {DEFAULT_PACKAGE_TIMEOUT_S})",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET_S,
        help=(
            f"seconds the whole gate may take (default {DEFAULT_BUDGET_S}). Set below the CI job's "
            f"timeout-minutes so the gate fails with a diagnostic rather than being killed without one"
        ),
    )
    args = parser.parse_args()

    allowlist = load_allowlist()
    used: set[tuple[str, str, int, int, str]] = set()
    total_killed = 0
    total_excused = 0
    all_failures: list[str] = []

    labels = [entry.label if isinstance(entry, ScopedPackage) else entry for entry in PERIMETER]
    print(f"Go mutation gate — perimeter: {', '.join(labels)}")
    print(f"gremlins {GREMLINS.rsplit('@', 1)[1]}, timeout-coefficient {args.timeout_coefficient}\n")

    started_at = time.monotonic()

    for entry in PERIMETER:
        if isinstance(entry, ScopedPackage):
            package, only_files, label = entry.package, entry.only_files, entry.label
        else:
            package, only_files, label = entry, (), entry

        left = remaining_budget(args.budget, started_at, time.monotonic())
        if left <= 0:
            print(f"  [FAIL] {label}: not started — the {args.budget:.0f}s gate budget was already spent")
            all_failures.append(f"{label}: gate budget exhausted before this package ran")
            break

        # Printed before the run, and flushed: if this package is the one that
        # hangs, this line is the only record of where the gate was. The
        # previous version printed only on completion, so a killed run said
        # nothing.
        print(f"  [ .. ] {label}: running (up to {package_timeout(args.package_timeout, left):.0f}s)", flush=True)

        package_started = time.monotonic()
        try:
            report = run_gremlins(
                package,
                args.timeout_coefficient,
                only_files,
                timeout_s=package_timeout(args.package_timeout, left),
            )
        except GateTimeoutError as timed_out:
            print(f"  [FAIL] {label}: {timed_out}")
            all_failures.append(f"{label}: {timed_out}")
            break

        elapsed = time.monotonic() - package_started
        killed, excused, failures = check_package(package, report, allowlist, used)
        total_killed += killed
        total_excused += excused
        all_failures.extend(failures)
        status = "FAIL" if failures else "ok"
        note = f", {excused} allowlisted-equivalent" if excused else ""
        print(
            f"  [{status:>4}] {label}: {killed} killed{note}, {len(failures)} unexcused survivor(s) in {elapsed:.0f}s"
        )

    stale = sorted(set(allowlist) - used)
    for key in stale:
        print(f"  [WARN] stale allowlist entry (no longer a survivor): {describe(key)}")

    # Elapsed against the budget, so a run drifting toward the ceiling is
    # visible in a passing log rather than only in the failing one.
    elapsed_total = time.monotonic() - started_at
    print(
        f"\nTotals: {total_killed} killed, {total_excused} allowlisted-equivalent, "
        f"{len(all_failures)} unexcused, {elapsed_total:.0f}s of {args.budget:.0f}s budget"
    )
    if all_failures:
        print("\nUnexcused mutants (kill with a test, or document as equivalent in mutation_equivalents.toml):")
        for loc in all_failures:
            print(f"  - {loc}")
        return 1
    print("Go mutation gate passed (every mutant killed or documented-equivalent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
