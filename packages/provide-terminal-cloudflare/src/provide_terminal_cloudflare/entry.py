from __future__ import annotations

import re
from urllib.parse import urlparse

try:
    from provide_terminal_cloudflare.cf_types import WorkerEntrypoint, json_response
    from provide_terminal_cloudflare.config import CloudflareConfig
    from provide_terminal_cloudflare.do.session_runtime import SessionRuntime
    from provide_terminal_cloudflare.ui.assets import serve_asset
except Exception:
    from cf_types import WorkerEntrypoint, json_response
    from config import CloudflareConfig
    from do.session_runtime import SessionRuntime
    from ui.assets import serve_asset

__all__ = ["Default", "SessionRuntime", "ProvideTerminalCloudflareWorker"]

_WORKER_ROUTE_PATTERNS = (
    re.compile(r"^/ws/browser/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/ws/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/ws/raw/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/term$"),
    re.compile(r"^/worker/(?P<worker_id>[a-zA-Z0-9_-]{1,64})/hijack(?:/.*)?$"),
)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        config = CloudflareConfig.from_env(self.env)
        path = urlparse(str(request.url)).path

        if path == "/api/health":
            return json_response(
                {
                    "ok": True,
                    "service": "provide-terminal-cloudflare",
                    "environment": config.environment,
                }
            )

        if path.startswith("/assets/"):
            return serve_asset(path.removeprefix("/assets/"))

        worker_id = _extract_worker_id(path)
        if worker_id is None:
            if path in {"/", "/app", "/app/"}:
                return serve_asset("hijack.html")
            return json_response({"error": "not_found", "path": path}, status=404)

        namespace = getattr(self.env, "SESSION_RUNTIME", None)
        if namespace is None:
            return json_response({"error": "SESSION_RUNTIME binding missing"}, status=500)

        stub_id = namespace.idFromName(worker_id)
        stub = namespace.get(stub_id)
        return await stub.fetch(request)


ProvideTerminalCloudflareWorker = Default


def _extract_worker_id(path: str) -> str | None:
    for pattern in _WORKER_ROUTE_PATTERNS:
        match = pattern.match(path)
        if match:
            return str(match.group("worker_id"))
    return None
