from __future__ import annotations

import importlib.resources

try:
    from provide_terminal_cloudflare.cf_types import Response
except Exception:
    from cf_types import Response

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def serve_asset(path: str) -> Response:
    rel = path.lstrip("/")
    try:
        frontend_root = importlib.resources.files("provide.terminal") / "frontend"
    except ModuleNotFoundError:
        return Response("asset package unavailable", status=404, headers={"content-type": "text/plain"})
    target = frontend_root / rel
    if not target.is_file():
        return Response("not found", status=404, headers={"content-type": "text/plain"})
    suffix = target.suffix.lower()
    mime = _MIME.get(suffix, "application/octet-stream")
    return Response(target.read_text(encoding="utf-8"), status=200, headers={"content-type": mime})
