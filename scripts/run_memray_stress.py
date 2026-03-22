#!/usr/bin/env python
"""Orchestrator for memray stress tests across all hot-path components."""

import subprocess
import sys
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent.parent / "memray-output"
SCRIPTS = [
    "memray_hub_stress.py",
    "memray_ansi_stress.py",
    "memray_gateway_stress.py",
]


def main() -> int:
    """Run all memray stress tests and print ready-to-paste commands."""
    OUTPUT_DIR.mkdir(exist_ok=True)

    for script in SCRIPTS:
        script_path = Path(__file__).parent / script
        output_bin = OUTPUT_DIR / f"{script.replace('.py', '')}.bin"

        print(f"\n>>> Running {script}...", file=sys.stderr)
        try:
            subprocess.run(  # noqa: S603
                ["uv", "run", "memray", "run", "-o", str(output_bin), str(script_path)],  # noqa: S607
                check=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"ERROR: {script} failed with exit code {e.returncode}", file=sys.stderr)
            return e.returncode

    # Print ready-to-paste commands
    print("\n" + "=" * 80)
    print("Memray stress tests complete. Ready-to-paste analysis commands:")
    print("=" * 80)
    for script in SCRIPTS:
        bin_file = OUTPUT_DIR / f"{script.replace('.py', '')}.bin"
        print(f"\n# {script}:")
        print(f"memray flamegraph {bin_file}")
        print(f"memray stats {bin_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
