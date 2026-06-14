#!/usr/bin/env python3
from __future__ import annotations

import configparser
import subprocess  # nosec
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path
from shutil import which

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.package_metadata import PUBLISHED_PACKAGES, PackageSpec  # noqa: E402


def _expected_frontend_files() -> tuple[str, ...]:
    """Discover all frontend files from the source tree at build time."""
    frontend = ROOT / "packages" / "provide-uterm-server" / "src" / "provide" / "uterm" / "server" / "frontend"
    if not frontend.exists():
        return ()
    return tuple(
        str(p.relative_to(ROOT / "packages" / "provide-uterm-server" / "src")).replace("\\", "/")
        for p in frontend.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not any(part.startswith(".") for part in p.parts)
    )


def _required_members(package: PackageSpec) -> tuple[str, ...]:
    if package.name == "provide-uterm-server":
        return (*package.required_members, *_expected_frontend_files())
    return package.required_members


def _build() -> None:
    uv = which("uv")
    if uv is None:
        raise RuntimeError("uv executable not found in PATH")
    for package in PUBLISHED_PACKAGES:
        subprocess.run([uv, "build", "--package", package.name], cwd=ROOT, check=True)


def _wheel_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


def _sdist_members(path: Path) -> set[str]:
    with tarfile.open(path, mode="r:gz") as tf:
        return {m.name for m in tf.getmembers() if m.isfile()}


def _assert_contains(members: set[str], required: tuple[str, ...], label: str) -> None:
    missing = []
    for req in required:
        if req.endswith("/"):
            if not any(req in name for name in members):
                missing.append(req)
        elif not any(name.endswith(req) for name in members):
            missing.append(req)
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"{label} missing required assets: {joined}")


def _read_wheel_text(wheel: Path, suffix: str) -> str:
    with zipfile.ZipFile(wheel) as zf:
        matches = [name for name in zf.namelist() if name.endswith(suffix)]
        if not matches:
            raise RuntimeError(f"{wheel.name} missing {suffix}")
        return zf.read(matches[0]).decode("utf-8")


def _assert_wheel_metadata(wheel: Path, package: PackageSpec) -> None:
    metadata = Parser().parsestr(_read_wheel_text(wheel, "/METADATA"))
    if metadata.get("Name") != package.name:
        raise RuntimeError(f"{wheel.name} has wrong metadata Name: {metadata.get('Name')!r}")
    if not metadata.get("Version"):
        raise RuntimeError(f"{wheel.name} missing metadata Version")
    for import_name in package.import_names:
        top_level = import_name.split(".", 1)[0]
        if top_level != "provide":
            raise RuntimeError(f"unexpected import root for {package.name}: {import_name}")


def _assert_entry_points(wheel: Path, package: PackageSpec) -> None:
    text = _read_wheel_text(wheel, "/entry_points.txt") if package.entry_points else ""
    parser = configparser.ConfigParser()
    parser.read_string(text or "[console_scripts]\n")
    scripts = dict(parser.items("console_scripts")) if parser.has_section("console_scripts") else {}
    missing = {name: target for name, target in package.entry_points.items() if scripts.get(name) != target}
    if missing:
        raise RuntimeError(f"{wheel.name} missing console entry point(s): {missing}")


def _artifact_pair(package: PackageSpec) -> tuple[Path, Path]:
    wheels = sorted(DIST.glob(f"{package.wheel_prefix}-*.whl"))
    sdists = sorted(DIST.glob(f"{package.wheel_prefix}-*.tar.gz"))
    if not wheels or not sdists:
        raise RuntimeError(f"expected {package.name} wheel and sdist in dist/")
    return wheels[-1], sdists[-1]


def main() -> int:
    _build()
    checked = 0
    for package in PUBLISHED_PACKAGES:
        wheel, sdist = _artifact_pair(package)
        wheel_members = _wheel_members(wheel)
        sdist_members = _sdist_members(sdist)
        required = _required_members(package)
        _assert_contains(wheel_members, required, f"{package.name} wheel")
        _assert_contains(sdist_members, required, f"{package.name} sdist")
        _assert_wheel_metadata(wheel, package)
        _assert_entry_points(wheel, package)
        checked += 1
    print(f"artifact verification passed ({checked} package(s))")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write(f"artifact verification failed: {exc}\n")
        raise SystemExit(1) from exc
