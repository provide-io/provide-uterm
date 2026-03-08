#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from shutil import which

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

REQUIRED_WHEEL = (
    "provide/terminal/frontend/hijack.js",
    "provide/terminal/frontend/hijack.css",
    "provide/terminal/frontend/hijack.html",
    "provide/terminal/frontend/app/boot.js",
    "provide/terminal/frontend/app/router.js",
    "provide/terminal/frontend/app/state.js",
    "provide/terminal/frontend/app/api.js",
    "provide/terminal/frontend/app/views/dashboard-view.js",
    "provide/terminal/frontend/app/views/operator-view.js",
    "provide/terminal/frontend/app/views/replay-view.js",
    "provide/terminal/frontend/app/views/session-view.js",
)


def _build() -> None:
    uv = which("uv")
    if uv is None:
        raise RuntimeError("uv executable not found in PATH")
    subprocess.run([uv, "build"], cwd=ROOT, check=True)  # noqa: S603


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
    wheels = sorted(DIST.glob("*.whl"))
    sdists = sorted(DIST.glob("*.tar.gz"))
    if not wheels or not sdists:
        raise RuntimeError("expected both wheel and sdist artifacts in dist/")

    wheel_members = _wheel_members(wheels[-1])
    sdist_members = _sdist_members(sdists[-1])
    _assert_contains(wheel_members, REQUIRED_WHEEL, "wheel")
    _assert_contains(sdist_members, REQUIRED_WHEEL, "sdist")
    print("artifact verification passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"artifact verification failed: {exc}\n")
        raise SystemExit(1) from exc
