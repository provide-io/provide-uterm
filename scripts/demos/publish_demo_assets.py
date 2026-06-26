#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Publish the recorded demo videos to the site's ``demo-assets`` GitHub release.

The demo mp4s (``demo/<feature>/<primary>_trim.mp4`` and the hero ``demo/reel.mp4``)
are regenerable build artifacts and are intentionally **not** committed to git
(see the ``demo/`` rules in ``.gitignore``). The site repo (``site-uterm-io``)
checks this repo out *fresh* in its deploy CI, so it cannot see those gitignored
videos. This script ships them out of band: it bundles the videos named by
``demo/site-manifest.json`` into a single tarball and uploads it to a durable
GitHub *release* that the site deploy downloads at build time.

The release is hosted on the **consumer** repo (``site-uterm-io``) on purpose:
provide-uterm's ``release.yml`` fires on ``release: published`` and would try to
publish packages to PyPI for *any* release, so we must not create releases here.
``site-uterm-io`` has no such pipeline and its own deploy CI can read the release
with the workflow ``GITHUB_TOKEN``.

Producer (this script) runs locally, where the recorded videos exist on disk::

    uv run python -m scripts.demos.publish_demo_assets            # build + upload
    uv run python -m scripts.demos.publish_demo_assets --dry-run  # build only

Requires the GitHub CLI (``gh``) authenticated with write access to the asset repo.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess  # nosec B404 - invoked only with a fixed argv (no shell)
import sys
import tarfile
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Asset-store coordinates. Overridable via env so the same script can target a
# fork or a staging repo; defaults live here (top of the module) rather than
# inline at the call sites.
ASSET_REPO = os.environ.get("DEMO_ASSETS_REPO", "provide-io/site-uterm-io")
RELEASE_TAG = os.environ.get("DEMO_ASSETS_TAG", "demo-assets")
TARBALL_NAME = "demo-assets.tar.gz"

# Where each demo's primary video and the hero reel land *inside* the tarball.
# This mirrors the layout the site's sync writes under static/demo-assets/, so
# the consumer can extract straight into that directory.
HERO_REEL_ARCNAME = "_hero/reel.mp4"
PER_DEMO_VIDEO_NAME = "demo.mp4"


def _stage_videos(demo_root: Path, staging: Path) -> list[str]:
    """Copy the manifest's primary videos + hero reel into ``staging``.

    Returns the list of arcnames staged (for logging). Missing videos are
    skipped with a warning rather than failing the whole publish — a partial
    upload still beats a broken site.
    """
    manifest_path = demo_root / "site-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    staged: list[str] = []
    for demo in manifest.get("demos", []):
        demo_id = demo["id"]
        video_rel = demo.get("artifacts", {}).get("video")
        if not video_rel:
            log.warning("%s: no video in manifest, skipping", demo_id)
            continue
        source = demo_root / video_rel
        if not source.is_file():
            log.warning("%s: video %s not on disk, skipping", demo_id, video_rel)
            continue
        arcname = f"{demo_id}/{PER_DEMO_VIDEO_NAME}"
        dest = staging / arcname
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        staged.append(arcname)

    reel = demo_root / "reel.mp4"
    if reel.is_file():
        dest = staging / HERO_REEL_ARCNAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(reel, dest)
        staged.append(HERO_REEL_ARCNAME)
    else:
        log.warning("hero reel %s not on disk, skipping", reel)

    return staged


def _build_tarball(staging: Path, out: Path) -> None:
    """Pack everything under ``staging`` into a deterministic gzip tarball."""
    with tarfile.open(out, "w:gz") as tar:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=str(path.relative_to(staging)))


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` subcommand, capturing output (no shell)."""
    return subprocess.run(  # nosec B603 - fixed executable, no shell, args are not user-derived
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _ensure_release() -> None:
    """Create the asset release if it does not already exist."""
    view = _gh("release", "view", RELEASE_TAG, "-R", ASSET_REPO)
    if view.returncode == 0:
        return
    log.info("creating release %s on %s", RELEASE_TAG, ASSET_REPO)
    created = _gh(
        "release",
        "create",
        RELEASE_TAG,
        "-R",
        ASSET_REPO,
        "--title",
        "Demo video assets",
        "--notes",
        "Out-of-band demo videos consumed by the site deploy. Regenerate with scripts/demos/publish_demo_assets.py.",
        "--latest=false",
    )
    if created.returncode != 0:
        raise RuntimeError(f"gh release create failed: {created.stderr.strip()}")


def _upload(tarball: Path) -> None:
    """Upload (clobbering) the tarball to the asset release."""
    result = _gh("release", "upload", RELEASE_TAG, "-R", ASSET_REPO, str(tarball), "--clobber")
    if result.returncode != 0:
        raise RuntimeError(f"gh release upload failed: {result.stderr.strip()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the tarball but do not touch GitHub.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Tarball output path (default: a temp file named {TARBALL_NAME}).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    repo_root = Path(__file__).resolve().parents[2]
    demo_root = repo_root / "demo"

    with tempfile.TemporaryDirectory(prefix="demo-assets-") as tmp:
        staging = Path(tmp) / "stage"
        staging.mkdir()
        staged = _stage_videos(demo_root, staging)
        if not staged:
            print("no videos staged — nothing to publish", file=sys.stderr)
            return 1

        out = args.out or (Path(tmp) / TARBALL_NAME)
        _build_tarball(staging, out)
        size_mb = out.stat().st_size / 1_000_000
        print(f"bundled {len(staged)} videos into {out.name} ({size_mb:.1f} MB)")

        if args.dry_run:
            print("--dry-run: skipping GitHub upload")
            if args.out:
                print(f"tarball left at {out}")
            return 0

        _ensure_release()
        _upload(out)
        print(f"uploaded {TARBALL_NAME} to {ASSET_REPO} release '{RELEASE_TAG}'")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
