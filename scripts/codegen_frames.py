#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate TypeScript types from the Pydantic frame schemas.

Pipeline
--------

1. Import ``AnyFrame`` from ``provide.uterm.bridge.schemas``.
2. Build a stable, deterministic JSON Schema with ``TypeAdapter.json_schema()``.
3. Write it to ``packages/provide-uterm-frontend/src/generated/frames.schema.json``.
4. Shell out to ``npx json-schema-to-typescript`` to produce
   ``packages/provide-uterm-frontend/src/generated/frames.ts``.
5. Prepend an SPDX header + AUTO-GENERATED banner.

Both outputs include explicit banners so accidental hand-edits are obvious.
The pre-commit hook + CI run this script with ``--check`` to fail if the
committed outputs drift from the Pydantic source.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from provide.uterm.bridge.schemas import AnyFrame

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_GEN_DIR = REPO_ROOT / "packages" / "provide-uterm-frontend" / "src" / "generated"
SCHEMA_PATH = FRONTEND_GEN_DIR / "frames.schema.json"
TS_PATH = FRONTEND_GEN_DIR / "frames.ts"

# REUSE-IgnoreStart — header constants below are emitted INTO generated files;
# they aren't license declarations for this script itself.
SPDX_HEADER_TS = (
    "//\n"
    "// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.\n"
    "// SPDX-License-Identifier: AGPL-3.0-or-later\n"
    "//\n"
    "// AUTO-GENERATED — DO NOT EDIT. Regenerate via scripts/codegen_frames.py.\n"
    "//\n"
)

SPDX_HEADER_JSON_BANNER = {
    "_banner": (
        "AUTO-GENERATED — DO NOT EDIT. Regenerate via scripts/codegen_frames.py. "
        "SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved. "
        "SPDX-License-Identifier: AGPL-3.0-or-later."
    )
}
# REUSE-IgnoreEnd


def _build_schema() -> dict:
    """Build the JSON Schema for ``AnyFrame`` with deterministic ordering."""
    adapter: TypeAdapter[object] = TypeAdapter(AnyFrame)
    schema = adapter.json_schema(by_alias=True, ref_template="#/definitions/{model}")
    # Pydantic v2 emits ``$defs`` by default; json-schema-to-typescript
    # accepts both, but we normalise to ``definitions`` for stability.
    if "$defs" in schema:
        schema["definitions"] = schema.pop("$defs")
    return {"title": "AnyFrame", **SPDX_HEADER_JSON_BANNER, **schema}


def _serialise_schema(schema: dict) -> str:
    """Dump the schema as canonical, sorted JSON for reproducibility."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def _run_json_schema_to_typescript(schema_path: Path, ts_path: Path) -> None:
    """Invoke ``json-schema-to-typescript`` via the locally-installed binary."""
    bin_path = REPO_ROOT / "node_modules" / ".bin" / "json2ts"
    if not bin_path.exists():
        raise SystemExit(
            "json-schema-to-typescript is not installed. Run `npm install` "
            "at the repo root after pulling the devDependency change."
        )
    cmd = [
        str(bin_path),
        "--input",
        str(schema_path),
        "--output",
        str(ts_path),
        "--no-additionalProperties",
        "--bannerComment",
        "",
        "--style.printWidth=120",
        "--style.tabWidth=2",
    ]
    subprocess.run(cmd, check=True)


def _wrap_ts_with_header(ts_path: Path) -> None:
    """Prepend the SPDX/banner header to the generated TS file."""
    current = ts_path.read_text(encoding="utf-8")
    if current.startswith(SPDX_HEADER_TS):
        return
    ts_path.write_text(SPDX_HEADER_TS + current, encoding="utf-8")


def _write_outputs(schema_path: Path, ts_path: Path) -> None:
    """Generate the schema + TS outputs into the given paths."""
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema = _build_schema()
    schema_path.write_text(_serialise_schema(schema), encoding="utf-8")
    _run_json_schema_to_typescript(schema_path, ts_path)
    _wrap_ts_with_header(ts_path)


def _files_equal(a: Path, b: Path) -> bool:
    return a.read_bytes() == b.read_bytes()


def _check_mode() -> int:
    """Regenerate into a temp dir; non-zero exit if committed files drift."""
    if not SCHEMA_PATH.exists() or not TS_PATH.exists():
        print(
            f"codegen_frames: missing output files at {FRONTEND_GEN_DIR}; "
            "run `python scripts/codegen_frames.py` and commit the results.",
            file=sys.stderr,
        )
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        tmp_schema = Path(tmp) / "frames.schema.json"
        tmp_ts = Path(tmp) / "frames.ts"
        _write_outputs(tmp_schema, tmp_ts)
        ok_schema = _files_equal(tmp_schema, SCHEMA_PATH)
        ok_ts = _files_equal(tmp_ts, TS_PATH)
        if ok_schema and ok_ts:
            return 0
        print(
            "codegen_frames: generated outputs differ from the committed files.\n"
            "Run `python scripts/codegen_frames.py` and commit the regenerated files.",
            file=sys.stderr,
        )
        if not ok_schema:
            print(f"  drift: {SCHEMA_PATH.relative_to(REPO_ROOT)}", file=sys.stderr)
        if not ok_ts:
            print(f"  drift: {TS_PATH.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed files match the current Pydantic source (non-zero on drift).",
    )
    args = parser.parse_args(argv)

    if shutil.which("node") is None:
        print("codegen_frames: node not found on PATH.", file=sys.stderr)
        return 2

    if args.check:
        return _check_mode()

    _write_outputs(SCHEMA_PATH, TS_PATH)
    print(f"codegen_frames: wrote {SCHEMA_PATH.relative_to(REPO_ROOT)}")
    print(f"codegen_frames: wrote {TS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
