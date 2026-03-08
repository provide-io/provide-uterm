from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

FrameType = Literal[
    "snapshot_req",
    "snapshot",
    "term",
    "input",
    "control",
    "hijack_state",
    "analysis",
    "error",
    "worker_connected",
    "worker_disconnected",
    # Browser-originated frames (heartbeat/ping keep-alives, WS-level hijack requests).
    # The CF backend routes hijack through REST; these arrive via WS from hijack.js.
    "heartbeat",
    "ping",
    "hijack_request",
    "hijack_release",
    "hijack_step",
    "hello",
]


class ProtocolError(ValueError):
    pass


class Frame(TypedDict, total=False):
    type: FrameType
    ts: float
    data: str
    screen: str
    action: str
    owner: str | None
    hijacked: bool
    lease_expires_at: float | None
    formatted: str
    message: str


@dataclass(slots=True)
class MessageLimits:
    max_ws_message_bytes: int = 1_048_576
    max_input_chars: int = 10_000


def parse_frame(raw: str, *, limits: MessageLimits | None = None) -> Frame:
    active_limits = limits or MessageLimits()
    if len(raw.encode("utf-8")) > active_limits.max_ws_message_bytes:
        raise ProtocolError("message too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid json") from exc
    if not isinstance(value, dict):
        raise ProtocolError("frame must be an object")
    frame_type = value.get("type")
    if not isinstance(frame_type, str):
        raise ProtocolError("missing frame type")

    normalized: Frame = {"type": frame_type, "ts": float(value.get("ts", time.time()))}

    if frame_type == "input":
        data = str(value.get("data", ""))
        if len(data) > active_limits.max_input_chars:
            raise ProtocolError("input too large")
        normalized["data"] = data
    elif frame_type == "snapshot":
        normalized["screen"] = str(value.get("screen", ""))
    elif frame_type == "term":
        normalized["data"] = str(value.get("data", ""))
    elif frame_type == "control":
        normalized["action"] = str(value.get("action", ""))
        normalized["owner"] = str(value.get("owner", "")) if value.get("owner") is not None else None
    elif frame_type == "analysis":
        normalized["formatted"] = str(value.get("formatted", ""))
    elif frame_type == "hijack_state":
        normalized["hijacked"] = bool(value.get("hijacked", False))
        normalized["owner"] = str(value.get("owner", "")) if value.get("owner") is not None else None
        lease_expires_at = value.get("lease_expires_at")
        normalized["lease_expires_at"] = float(lease_expires_at) if lease_expires_at is not None else None
    elif frame_type in {
        "snapshot_req",
        "error",
        "worker_connected",
        "worker_disconnected",
        "heartbeat",
        "ping",
        "hijack_request",
        "hijack_release",
        "hijack_step",
        "hello",
    }:
        pass
    else:
        raise ProtocolError(f"unsupported frame type: {frame_type}")

    return normalized


def frame_json(frame_type: FrameType, **kwargs: Any) -> str:
    payload = {"type": frame_type, "ts": time.time(), **kwargs}
    return json.dumps(payload, ensure_ascii=True)
