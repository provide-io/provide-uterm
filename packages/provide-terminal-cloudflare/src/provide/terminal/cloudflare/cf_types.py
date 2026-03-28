from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CFWebSocket(Protocol):
    """Structural type for CF Durable Object WebSocket handles (Pyodide JS proxy)."""

    def send(self, message: str) -> Any: ...  # returns Awaitable | None in Pyodide
    def close(self, code: int = 1000, reason: str = "") -> None: ...
    def serializeAttachment(self, attachment: str) -> None: ...  # noqa: N802
    def deserializeAttachment(self) -> Any: ...  # noqa: N802


try:
    from workers import DurableObject, Response, WorkerEntrypoint  # type: ignore
except Exception:  # pragma: no cover

    class DurableObject:  # pragma: no cover
        def __init__(self, *args: Any, **kwargs: Any):
            ctx = kwargs.get("ctx")
            env = kwargs.get("env")
            if len(args) >= 1:
                ctx = args[0]
            if len(args) >= 2:
                env = args[1]
            self.ctx = ctx
            self.env = env

    class WorkerEntrypoint:  # pragma: no cover
        def __init__(self, *args: Any, **kwargs: Any):
            env = kwargs.get("env")
            if len(args) >= 2:
                env = args[1]
            elif len(args) == 1:
                env = args[0]
            self.env = env

    @dataclass(slots=True)
    class Response:  # pragma: no cover
        body: str | None
        status: int = 200
        headers: dict[str, str] | None = None
        web_socket: CFWebSocket | None = None

        @classmethod
        def json(cls, data: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> Response:
            merged_headers = {"content-type": "application/json"}
            if headers:
                merged_headers.update(headers)
            return cls(json.dumps(data, ensure_ascii=True), status=status, headers=merged_headers)


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    if hasattr(Response, "json"):
        return Response.json(data, status=status, headers=headers)  # type: ignore[attr-defined]
    body = json.dumps(data, ensure_ascii=True)  # pragma: no cover
    merged_headers = {"content-type": "application/json"}  # pragma: no cover
    if headers:  # pragma: no cover
        merged_headers.update(headers)  # pragma: no cover
    return Response(body, status=status, headers=merged_headers)  # pragma: no cover
