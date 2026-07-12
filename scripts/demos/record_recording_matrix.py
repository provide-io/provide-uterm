#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Record multi-language session-recording demos (Python / Go / C#) via asciinema.

Outputs:
  demo/recording/python/terminal.cast
  demo/recording/go/terminal.cast
  demo/recording/csharp/terminal.cast

Usage (repo root):
  uv run python -m scripts.demos.record_recording_matrix
  uv run python -m scripts.demos.record_recording_matrix --langs python,go
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess  # nosec
import sys
from pathlib import Path

from scripts.demos.output import BASE_OUT, banner, info, ok, warn

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURE = "recording"
LANGS = ("python", "go", "csharp")


def _asciinema_cmd(
    command: str,
    out_path: Path,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Path | None:
    """Run ``asciinema rec -c <command>`` into *out_path*."""
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Absolute stderr path so `cd` in the command cannot break the redirect.
    stderr_log = out_path.with_suffix(out_path.suffix + ".stderr")
    full = f"{command} 2>{shlex.quote(str(stderr_log))}"
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    work = Path(cwd) if cwd is not None else REPO_ROOT
    try:
        subprocess.run(
            [
                "asciinema",
                "rec",
                str(out_path),
                "--overwrite",
                "-c",
                full,
            ],
            check=True,
            timeout=180,
            env=run_env,
            cwd=str(work),
        )
        stderr_log.unlink(missing_ok=True)
        return out_path
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        warn(f"asciinema failed ({out_path.name}): {exc}")
        if stderr_log.is_file():
            warn(f"stderr log: {stderr_log}")
        return None


def record_python(out: Path) -> Path | None:
    demo = REPO_ROOT / "scripts" / "demos" / "recording_matrix" / "demo_python.py"
    env = {
        "PYTHONPATH": str(REPO_ROOT) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
    }
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(demo))}"
    return _asciinema_cmd(cmd, out, env=env)


def record_go(out: Path) -> Path | None:
    go_mod = REPO_ROOT / "packages" / "provide-uterm-go"
    # Run from module root so go.mod resolves (absolute stderr via helper).
    return _asciinema_cmd("go run ./cmd/demo-recording", out, cwd=go_mod)


def record_csharp(out: Path) -> Path | None:
    proj = REPO_ROOT / "packages" / "provide-uterm-csharp" / "cmd" / "RecordingDemo" / "RecordingDemo.csproj"
    # Prefer DOTNET_ROOT-aware `dotnet` on PATH.
    cmd = f"dotnet run --project {shlex.quote(str(proj))} -c Release --no-restore 2>/dev/null || dotnet run --project {shlex.quote(str(proj))} -c Release"
    # Build first so cast is clean.
    try:
        subprocess.run(
            ["dotnet", "build", str(proj), "-c", "Release", "-v", "q"],
            check=True,
            timeout=120,
            cwd=str(REPO_ROOT),
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        warn(f"dotnet build failed: {exc}")
        return None
    cmd = f"dotnet run --project {shlex.quote(str(proj))} -c Release --no-build"
    return _asciinema_cmd(cmd, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--langs",
        default="python,go,csharp",
        help="comma-separated subset of python,go,csharp",
    )
    parser.add_argument(
        "--base-out",
        type=Path,
        default=BASE_OUT,
        help="demo output root (default: demo/)",
    )
    args = parser.parse_args(argv)
    wanted = {x.strip().lower() for x in args.langs.split(",") if x.strip()}
    unknown = wanted - set(LANGS)
    if unknown:
        warn(f"unknown langs: {', '.join(sorted(unknown))}")
        return 2

    if not shutil.which("asciinema"):
        warn("asciinema not found on PATH")
        return 1

    banner("Session recording multi-language matrix")
    results: dict[str, Path | None] = {}
    base = args.base_out / FEATURE

    for lang in LANGS:
        if lang not in wanted:
            continue
        cast = base / lang / "terminal.cast"
        info(f"recording {lang} → {cast}")
        if lang == "python":
            results[lang] = record_python(cast)
        elif lang == "go":
            results[lang] = record_go(cast)
        elif lang == "csharp":
            results[lang] = record_csharp(cast)

    print()
    ok_count = 0
    for lang, path in results.items():
        if path and path.is_file():
            ok(f"{lang}: {path} ({path.stat().st_size} bytes)")
            ok_count += 1
        else:
            warn(f"{lang}: FAILED")

    # Keep legacy demo/recording/terminal.cast pointing at python matrix cast when present.
    py_cast = results.get("python")
    legacy = base / "terminal.cast"
    if py_cast and py_cast.is_file():
        shutil.copy2(py_cast, legacy)
        ok(f"legacy terminal.cast refreshed from python matrix ({legacy})")

    print()
    if ok_count == len(results) and ok_count > 0:
        ok(f"matrix complete: {ok_count}/{len(results)} languages")
        return 0
    warn(f"matrix incomplete: {ok_count}/{len(results)} languages")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
