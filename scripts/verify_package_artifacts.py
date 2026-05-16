#!/usr/bin/env python3
from __future__ import annotations

import subprocess  # nosec
import sys
import tarfile
import zipfile
from pathlib import Path
from shutil import which

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"


def _expected_frontend_files() -> tuple[str, ...]:
    """Discover all frontend files from the source tree at build time."""
    frontend = ROOT / "packages" / "provide-uterm-server" / "src" / "provide" / "terminal" / "server" / "frontend"
    return tuple(
        str(p.relative_to(ROOT / "packages" / "provide-uterm-server" / "src")).replace("\\", "/")
        for p in frontend.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")
    )


def _build() -> None:
    uv = which("uv")
    if uv is None:
        raise RuntimeError("uv executable not found in PATH")
    # Build the server package (which owns the frontend assets)
    subprocess.run([uv, "build", "--package", "provide-uterm-server"], cwd=ROOT, check=True)


def _wheel_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def _sdist_members(path: Path) -> set[str]:
    with tarfile.open(path, mode="r:gz") as tf:
        return {m.name for m in tf.getmembers() if m.isfile()}


def _assert_contains(members: set[str], required: tuple[str, ...], label: str) -> None:
    missing = [req for req in required if not any(name.endswith(req) for name in members)]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"{label} missing required assets: {joined}")


def main() -> int:
    _build()
    # Filter to provide-uterm-server artifacts only (not workspace meta-package)
    wheels = sorted(DIST.glob("provide_uterm_server-*.whl"))
    sdists = sorted(DIST.glob("provide_uterm_server-*.tar.gz"))
    if not wheels or not sdists:
        raise RuntimeError("expected provide-uterm-server wheel and sdist in dist/")

    required = _expected_frontend_files()
    if not required:
        raise RuntimeError(
            "no frontend files found in packages/provide-uterm-server/src/provide/uterm/server/frontend/"
        )

    wheel_members = _wheel_members(wheels[-1])
    sdist_members = _sdist_members(sdists[-1])
    _assert_contains(wheel_members, required, "wheel")
    _assert_contains(sdist_members, required, "sdist")
    print(f"artifact verification passed ({len(required)} frontend files)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"artifact verification failed: {exc}\n")
        raise SystemExit(1) from exc
