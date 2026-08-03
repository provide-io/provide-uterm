# CM-12: Freshness-Safe Go Live Driver

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the live harness silently executing a stale prebuilt Go driver,
so a green live matrix cannot reflect code that is no longer in the tree.

**Architecture:** The harness prefers a prebuilt binary if the file exists, with
no freshness check at all. Replace "exists" with "exists and is newer than every
source it was built from," falling back to a fresh build otherwise.

**Tech Stack:** Python (harness), Go toolchain.

## Global Constraints

- Behavior when the binary is fresh does not change — the fast path stays fast.
  A live matrix that rebuilds unconditionally would add a Go build to every run.
- SPDX headers on new files.
- The change is in the harness, not in CI configuration, so it protects local
  runs too. A local green that CI cannot reproduce is the exact failure this
  addresses.

## Context

`conformance/live/harness/registry.py:86-87`:

```python
    built = package / "bin/uterm-live-driver"
    command = (str(built),) if built.exists() else ("go", "run", "./cmd/uterm-live-driver")
```

`built.exists()` is the entire check. If the file is present it is executed, no
matter how old.

Compounding it: `packages/provide-uterm-go/bin/` is **untracked**.

```
$ git ls-files packages/provide-uterm-go/bin/
(no output)

$ ls packages/provide-uterm-go/bin/
uterm  uterm-live-driver  uterm-manager  uterm-mcp
```

So the binary is a local artifact with no defined provenance and no lifecycle.
A developer who built it once, then changed Go source, then ran the live matrix,
gets results from the old build — and the matrix reports green for code that no
longer exists.

This is the same class of defect as the frontend build-output staleness already
recorded in this repo's history, where gitignored build output masked a broken
build locally while CI failed.

The quality-evidence design states the requirement: "The live harness never
silently executes a stale repository binary. It builds a fresh driver into a
temporary or content-addressed path, or validates source and module timestamps
before reuse. Tests modify a relevant source timestamp or fingerprint and prove
that stale output cannot be selected."

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-12.

## File Structure

- `conformance/live/harness/registry.py` — the resolution logic.
- `conformance/live/harness/freshness.py` — new. The staleness check, separated
  so it is testable without standing up a driver.
- `tests/conformance/live/test_drivers.py` — extend with the staleness tests.

---

### Task 1: A testable staleness check

**Files:**
- Create: `conformance/live/harness/freshness.py`
- Modify: `tests/conformance/live/test_drivers.py`

**Interfaces:**
- Produces:
  ```python
  def is_stale(binary: Path, sources: Iterable[Path]) -> bool: ...
  def go_sources(package_root: Path) -> list[Path]: ...
  ```
  Task 2 consumes both.

- [ ] **Step 1: Write the failing test**

Add to `tests/conformance/live/test_drivers.py`:

```python
def test_binary_newer_than_all_sources_is_fresh(tmp_path: Path) -> None:
    source = tmp_path / "main.go"
    source.write_text("package main\n")
    binary = tmp_path / "driver"
    binary.write_bytes(b"")
    os.utime(source, (1000, 1000))
    os.utime(binary, (2000, 2000))

    assert not is_stale(binary, [source])


def test_binary_older_than_a_source_is_stale(tmp_path: Path) -> None:
    # The defect, directly: source edited after the binary was built.
    source = tmp_path / "main.go"
    source.write_text("package main\n")
    binary = tmp_path / "driver"
    binary.write_bytes(b"")
    os.utime(binary, (1000, 1000))
    os.utime(source, (2000, 2000))

    assert is_stale(binary, [source])


def test_binary_older_than_any_one_source_is_stale(tmp_path: Path) -> None:
    # A single edited file among many must be enough. Checking only the
    # newest-at-build-time source would miss it.
    binary = tmp_path / "driver"
    binary.write_bytes(b"")
    os.utime(binary, (2000, 2000))

    sources = []
    for i in range(5):
        s = tmp_path / f"f{i}.go"
        s.write_text("package main\n")
        os.utime(s, (1000, 1000))
        sources.append(s)
    os.utime(sources[3], (3000, 3000))

    assert is_stale(binary, sources)


def test_missing_binary_is_stale(tmp_path: Path) -> None:
    source = tmp_path / "main.go"
    source.write_text("package main\n")

    assert is_stale(tmp_path / "absent", [source])


def test_no_sources_is_stale(tmp_path: Path) -> None:
    # An empty source list means the enumeration failed. Treating that as fresh
    # would make a broken glob look like a passing check — which is how the
    # original defect reads a green matrix.
    binary = tmp_path / "driver"
    binary.write_bytes(b"")

    assert is_stale(binary, [])


def test_go_sources_finds_module_files(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "a.go").write_text("package main\n")
    nested = tmp_path / "cmd" / "driver"
    nested.mkdir(parents=True)
    (nested / "main.go").write_text("package main\n")

    found = {p.name for p in go_sources(tmp_path)}

    assert "go.mod" in found
    assert "a.go" in found
    assert "main.go" in found
```

Add the imports the tests need (`os`, `Path`, and the two functions).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/conformance/live/test_drivers.py -k "stale or go_sources" -v`

Expected: FAIL — `conformance.live.harness.freshness` does not exist.

- [ ] **Step 3: Write the implementation**

Create `conformance/live/harness/freshness.py`:

```python
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Decide whether a prebuilt live driver can be trusted.

The harness used to run any binary that existed, so a driver built before the
last source edit produced a green live matrix for code no longer in the tree.
``bin/`` is untracked, so that binary has no provenance and no lifecycle — it is
whatever the last local build left behind.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Files whose change should invalidate a build. Module metadata counts: a
# dependency bump changes the binary without touching a single .go file.
_SOURCE_SUFFIXES = (".go",)
_SOURCE_NAMES = ("go.mod", "go.sum")

_SKIP_DIRS = {"bin", "testdata", ".git"}


def go_sources(package_root: Path) -> list[Path]:
    """Every file that, if changed, means a prebuilt driver is out of date."""
    found: list[Path] = []
    for path in package_root.rglob("*"):
        if not path.is_file():
            continue
        if _SKIP_DIRS.intersection(path.relative_to(package_root).parts):
            continue
        if path.suffix in _SOURCE_SUFFIXES or path.name in _SOURCE_NAMES:
            found.append(path)
    return found


def is_stale(binary: Path, sources: Iterable[Path]) -> bool:
    """True when ``binary`` cannot be trusted to reflect ``sources``.

    Fails closed. An absent binary, an empty source list, or an unreadable
    timestamp all return True: the cost of an unnecessary rebuild is seconds,
    and the cost of a false green is a matrix that certifies deleted code.
    """
    source_list = list(sources)
    if not source_list:
        return True

    try:
        built_at = binary.stat().st_mtime
    except OSError:
        return True

    for source in source_list:
        try:
            if source.stat().st_mtime > built_at:
                return True
        except OSError:
            return True

    return False
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/conformance/live/test_drivers.py -k "stale or go_sources" -v`

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add conformance/live/harness/freshness.py tests/conformance/live/test_drivers.py
git commit -m "feat(conformance): decide whether a prebuilt live driver is stale

Fails closed on every uncertainty — absent binary, empty source list,
unreadable timestamp. An empty source list in particular must not read
as fresh: a broken glob would then make 'nothing to check' look like
'nothing is wrong', which is the shape of the bug this replaces.

go.mod and go.sum count as sources. A dependency bump changes the binary
without touching a single .go file."
```

---

### Task 2: The harness refuses a stale binary

**Files:**
- Modify: `conformance/live/harness/registry.py:86-87`
- Modify: `tests/conformance/live/test_drivers.py`

**Interfaces:**
- Consumes: `is_stale`, `go_sources` from Task 1.
- Produces: no signature change to the registry's public surface.

- [ ] **Step 1: Write the failing test**

Add to `tests/conformance/live/test_drivers.py`:

```python
def test_stale_prebuilt_driver_is_not_selected(tmp_path: Path, monkeypatch) -> None:
    # The exact defect: a binary exists but a source has been edited since. The
    # harness must fall back to building rather than run it.
    package = tmp_path / "packages" / "provide-uterm-go"
    (package / "bin").mkdir(parents=True)
    (package / "cmd" / "uterm-live-driver").mkdir(parents=True)

    binary = package / "bin" / "uterm-live-driver"
    binary.write_bytes(b"")
    os.utime(binary, (1000, 1000))

    source = package / "cmd" / "uterm-live-driver" / "main.go"
    source.write_text("package main\n")
    os.utime(source, (2000, 2000))

    command = resolve_go_driver_command(package)

    assert str(binary) not in command
    assert "go" in command


def test_fresh_prebuilt_driver_is_selected(tmp_path: Path) -> None:
    package = tmp_path / "packages" / "provide-uterm-go"
    (package / "bin").mkdir(parents=True)
    (package / "cmd" / "uterm-live-driver").mkdir(parents=True)

    source = package / "cmd" / "uterm-live-driver" / "main.go"
    source.write_text("package main\n")
    os.utime(source, (1000, 1000))

    binary = package / "bin" / "uterm-live-driver"
    binary.write_bytes(b"")
    os.utime(binary, (2000, 2000))

    command = resolve_go_driver_command(package)

    assert command == (str(binary),)
```

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/conformance/live/test_drivers.py -k "prebuilt" -v`

Expected: `test_stale_prebuilt_driver_is_not_selected` FAILS — the current code
selects the binary because it exists. That failure *is* the defect.

- [ ] **Step 3: Extract and fix the resolution**

In `conformance/live/harness/registry.py`, replace lines 86-87 with a call to a
named function, and define it in the same module:

```python
def resolve_go_driver_command(package: Path) -> tuple[str, ...]:
    """Prefer a prebuilt driver only when it is newer than every Go source.

    This used to be `if built.exists()`, with no freshness check at all. Since
    `bin/` is untracked, that binary is whatever the last local build left
    behind — so a developer who built once, edited Go source, then ran the live
    matrix got a green result for code that was no longer in the tree.
    """
    built = package / "bin/uterm-live-driver"
    if not is_stale(built, go_sources(package)):
        return (str(built),)
    return ("go", "run", "./cmd/uterm-live-driver")
```

Import `is_stale` and `go_sources` from `freshness`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/conformance/live/test_drivers.py -v`

Expected: PASS, including the pre-existing driver tests.

- [ ] **Step 5: Verify against the real tree**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
uv run python -c "
from pathlib import Path
from conformance.live.harness.registry import resolve_go_driver_command
print(resolve_go_driver_command(Path('packages/provide-uterm-go')))
"
touch packages/provide-uterm-go/hub/connection.go
uv run python -c "
from pathlib import Path
from conformance.live.harness.registry import resolve_go_driver_command
print(resolve_go_driver_command(Path('packages/provide-uterm-go')))
"
```

Expected: the second call returns the `go run` fallback where the first may have
returned the binary. That is the check working against the real repository, not
just a temp directory.

- [ ] **Step 6: Commit**

```bash
git add conformance/live/harness/registry.py tests/conformance/live/test_drivers.py
git commit -m "fix(conformance): never run a live driver older than its sources

The harness ran any binary that existed. bin/ is untracked, so that
binary is whatever the last local build left behind — build once, edit
Go source, run the live matrix, and it reports green for code that is no
longer in the tree.

Prefer the prebuilt driver only when it is newer than every Go source
and module file. Verified against the real tree by touching a source and
watching the resolution switch to a fresh build."
```

---

### Task 3: Full live-matrix run

**Files:**
- Verify only.

- [ ] **Step 1: Run the live matrix with a deliberately stale binary**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
touch -t 202001010000 packages/provide-uterm-go/bin/uterm-live-driver
uv run pytest tests/conformance/live/ -v 2>&1 | tail -20
```

Expected: PASS, having built a fresh driver rather than running the 2020-dated
one. Confirm from the output that a build occurred.

- [ ] **Step 2: Run it normally**

Run:
```bash
cd packages/provide-uterm-go && go build -o bin/uterm-live-driver ./cmd/uterm-live-driver
cd /Volumes/data/pyv/provide-uterm
uv run pytest tests/conformance/live/ -v 2>&1 | tail -20
```

Expected: PASS, using the freshly built binary — the fast path still works.

- [ ] **Step 3: Confirm the known live-matrix state**

Run `30860934627` (`814f6d87`, 2026-08-03) reported two `008_rate_limits`
failures and four unsupported cells. If those are still present, they are not
caused by this change — CM-07 owns the unsupported cells, and the rate-limit
failures are tracked separately. Note what you observe rather than assuming.

- [ ] **Step 4: Commit if anything changed**

If Steps 1-3 required no changes, there is nothing to commit. Do not make an
empty commit.

---

## Definition of done

Per the measurement spec, CM-12 closes when:

- `test_stale_prebuilt_driver_is_not_selected` was observed failing against the
  `built.exists()` version;
- the check was verified against the real tree by touching a Go source and
  watching resolution switch to a fresh build;
- the live matrix passes with a deliberately back-dated binary, having rebuilt;
- the fresh-binary fast path still selects the prebuilt driver.

Then update the CM-12 row and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- Mtime comparison is the design's own suggested approach ("validates source and
  module timestamps before reuse"). It is not perfect — a checkout can produce
  mtimes in any order — but it fails closed, so the error is a wasted rebuild
  rather than a false green. If mtime proves unreliable in CI, the alternative
  the design offers is a content-addressed path, which is a larger change.
- Consider whether `packages/provide-uterm-go/bin/` should be gitignored
  explicitly rather than merely untracked. An untracked directory that
  everything depends on is worth naming in `.gitignore` so its status is a
  decision rather than an accident.
- The other three live drivers (Python, C#, TypeScript) may have the same
  pattern. Check before closing this finding: `grep -n "exists()" conformance/live/harness/registry.py`.
