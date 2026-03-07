from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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
            if len(args) >= 1:
                env = args[-1]
            self.env = env

    @dataclass(slots=True)
    class Response:  # pragma: no cover
        body: str
        status: int = 200
        headers: dict[str, str] | None = None

        @classmethod
        def json(cls, data: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> Response:
            merged_headers = {"content-type": "application/json"}
            if headers:
                merged_headers.update(headers)
            return cls(json.dumps(data, ensure_ascii=True), status=status, headers=merged_headers)


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    if hasattr(Response, "json"):
        return Response.json(data, status=status, headers=headers)  # type: ignore[attr-defined]
    body = json.dumps(data, ensure_ascii=True)
    merged_headers = {"content-type": "application/json"}
    if headers:
        merged_headers.update(headers)
    return Response(body, status=status, headers=merged_headers)
