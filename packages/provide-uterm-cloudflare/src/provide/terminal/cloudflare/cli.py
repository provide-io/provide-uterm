from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def _package_root() -> Path:
    # cli.py lives at src/provide/terminal/cloudflare/cli.py; go up 4 levels to package root
    return Path(__file__).resolve().parents[4]


def _run(cmd: list[str], cwd: Path) -> int:
    proc = subprocess.run(cmd, cwd=cwd)
    return int(proc.returncode)


def _require_pywrangler() -> None:
    if shutil.which("pywrangler") is None:
        raise RuntimeError("pywrangler not found; install workers-py")


def cmd_build(_args: argparse.Namespace) -> int:
    pkg_root = _package_root()
    entry_dir = pkg_root / "src" / "provide" / "terminal" / "cloudflare" / "entry"
    required = [pkg_root / "wrangler.toml", entry_dir / "__init__.py"]
    missing = [path for path in required if not path.exists()]
    if missing:
        for path in missing:
            print(f"missing required file: {path}", file=sys.stderr)
        return 1
    print("build validation ok")
    return 0


def cmd_dev(args: argparse.Namespace) -> int:
    _require_pywrangler()
    pkg_root = _package_root()
    cmd = ["pywrangler", "dev", "--ip", args.ip, "--port", str(args.port)]
    return _run(cmd, cwd=pkg_root)


def cmd_deploy(args: argparse.Namespace) -> int:
    _require_pywrangler()
    pkg_root = _package_root()
    cmd = ["pywrangler", "deploy", "--env", args.env]
    if args.extra:
        cmd.extend(shlex.split(args.extra))
    return _run(cmd, cwd=pkg_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uterm-cf")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="validate package layout and worker entry")
    p_build.set_defaults(func=cmd_build)

    p_dev = sub.add_parser("dev", help="run pywrangler dev")
    p_dev.add_argument("--ip", default="127.0.0.1")
    p_dev.add_argument("--port", default=8787, type=int)
    p_dev.set_defaults(func=cmd_dev)

    p_deploy = sub.add_parser("deploy", help="run pywrangler deploy")
    p_deploy.add_argument("--env", default="production")
    p_deploy.add_argument("--extra", default="")
    p_deploy.set_defaults(func=cmd_deploy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
