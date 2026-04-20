from __future__ import annotations

import importlib.resources
from pathlib import Path

try:
    from provide.terminal.cloudflare.cf_types import Response
except Exception:
    from cf_types import Response  # type: ignore[import-not-found]

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# Static directory co-located with this file (populated at build time in Docker).
_LOCAL_STATIC = Path(__file__).parent / "static"


def read_asset_text(path: str) -> str | None:
    """Return the raw text content of an asset, or None if not found."""
    rel = path.lstrip("/")
    if ".." in rel.split("/"):
        return None
    try:
        local_root = importlib.resources.files("provide.terminal.cloudflare.ui") / "static"
        local_target = local_root / rel
        if local_target.is_file():
            return local_target.read_text(encoding="utf-8")
    except (ModuleNotFoundError, TypeError):
        pass
    try:
        local_target2 = _LOCAL_STATIC / rel
        if local_target2.is_file():
            return local_target2.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        frontend_root = importlib.resources.files("provide.terminal.server") / "frontend"
        target = frontend_root / rel
        if target.is_file():
            return target.read_text(encoding="utf-8")
    except (ModuleNotFoundError, TypeError):
        pass
    return None


def serve_asset(path: str) -> Response:
    rel = path.lstrip("/")
    if ".." in rel.split("/"):
        return Response("forbidden", status=403, headers={"content-type": "text/plain"})

    # 1. Try importlib.resources (works when package is properly installed in CPython).
    try:
        local_root = importlib.resources.files("provide.terminal.cloudflare.ui") / "static"
        local_target = local_root / rel
        if local_target.is_file():
            suffix = Path(local_target.name).suffix.lower()
            mime = _MIME.get(suffix, "application/octet-stream")
            return Response(local_target.read_text(encoding="utf-8"), status=200, headers={"content-type": mime})
    except (ModuleNotFoundError, TypeError):
        pass

    # 2. Try __file__-relative path (works in pywrangler dev when static dir is populated).
    try:
        local_target2 = _LOCAL_STATIC / rel
        if local_target2.is_file():
            suffix = local_target2.suffix.lower()
            mime = _MIME.get(suffix, "application/octet-stream")
            return Response(local_target2.read_text(encoding="utf-8"), status=200, headers={"content-type": mime})
    except OSError:
        pass

    # 3. Fall back to the main provide-terminal package (installed alongside this package).
    try:
        frontend_root = importlib.resources.files("provide.terminal.server") / "frontend"
    except ModuleNotFoundError:
        return Response("asset package unavailable", status=404, headers={"content-type": "text/plain"})
    target = frontend_root / rel
    if not target.is_file():
        return Response("not found", status=404, headers={"content-type": "text/plain"})
    suffix = Path(target.name).suffix.lower()
    mime = _MIME.get(suffix, "application/octet-stream")
    return Response(target.read_text(encoding="utf-8"), status=200, headers={"content-type": mime})
