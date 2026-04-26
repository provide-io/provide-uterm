from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from provide.terminal.bridge.contracts import (
    Frame,
    FrameType,
    SessionStatusResponse,
)

try:
    from provide.terminal.control_channel import (
        ControlChannelDecoder,
        ControlChannelProtocolError,
        ControlChunk,
        DataChunk,
        encode_control,
        encode_data,
    )
except (ImportError, ModuleNotFoundError):  # pragma: no cover
    # Fallback for Cloudflare Durable Objects validation phase
    ControlChunk = Any  # type: ignore[assignment]
    ControlChannelDecoder = Any  # type: ignore[assignment]
    ControlChannelProtocolError = Exception  # type: ignore[assignment]
    DataChunk = Any  # type: ignore[assignment]

    def encode_control(*_a: Any, **_k: Any) -> bytes:  # type: ignore[assignment]
        return b""

    def encode_data(*_a: Any, **_k: Any) -> bytes:  # type: ignore[assignment]
        return b""


if TYPE_CHECKING:
    from provide.terminal.cloudflare.cf_types import CFWebSocket

# ---------------------------------------------------------------------------
# REST API response contracts
#
# These TypedDicts are the canonical schema for CF REST responses and must
# stay in sync with the FastAPI backend (provide-terminal SessionRuntimeStatus
# and hijack route responses).  Any field added to the FastAPI schema must
# also be added here, with an appropriate CF default documented inline.
# ---------------------------------------------------------------------------


class SessionStatusItem(SessionStatusResponse):
    """Shape of each item in GET /api/sessions.

    Mirrors ``provide-terminal`` ``SessionRuntimeStatus``/``SessionStatus`` (TS).
    CF-only fields: ``hijacked``.
    Metadata fields (display_name, connector_type, created_at, tags, visibility,
    owner) are loaded from KV on first contact and persisted to DO SQLite.
    """


class ProtocolError(ValueError):
    pass


@dataclass(slots=True)
class MessageLimits:
    max_ws_message_bytes: int = 1_048_576
    max_input_chars: int = 10_000


def _normalize_frame(value: dict[str, Any], *, limits: MessageLimits) -> Frame:
    frame_type = value.get("type")
    if not isinstance(frame_type, str):
        raise ProtocolError("missing frame type")

    normalized: Frame = {"type": frame_type, "ts": float(value.get("ts", time.time()))}

    if frame_type == "input":
        data = str(value.get("data", ""))
        if len(data) > limits.max_input_chars:
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
    elif frame_type == "worker_hello":
        mode = value.get("input_mode")
        if mode in {"hijack", "open"}:
            normalized["mode"] = mode
        if "protocol_version" in value:
            try:
                normalized["protocol_version"] = int(value["protocol_version"])
            except (ValueError, TypeError):
                pass
    elif frame_type == "resume":
        normalized["token"] = str(value.get("token", ""))
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
    elif frame_type in {
        # DeckMux presence messages — relayed verbatim between browsers.
        "presence_update",
        "presence_sync",
        "presence_leave",
        "queued_input",
        "control_request",
        # HTTP intercept commands — relayed browser→worker.
        "http_action",
        "http_intercept_toggle",
        "http_inspect_toggle",
    }:
        # Copy all non-type, non-ts fields through so the relay is lossless.
        for k, v in value.items():
            if k not in ("type", "ts"):
                normalized[k] = v
    else:
        raise ProtocolError(f"unsupported frame type: {frame_type}")

    return normalized


def parse_stream(
    raw: str,
    *,
    data_frame_type: Literal["input", "term"],
    limits: MessageLimits | None = None,
) -> list[Frame]:
    active_limits = limits or MessageLimits()
    if len(raw.encode("utf-8")) > active_limits.max_ws_message_bytes:
        raise ProtocolError("message too large")
    decoder = ControlChannelDecoder(max_control_payload_bytes=active_limits.max_ws_message_bytes)
    try:
        events = decoder.feed(raw)
        events.extend(decoder.finish())
    except ControlChannelProtocolError as exc:
        raise ProtocolError(str(exc)) from exc
    if not events:
        raise ProtocolError("empty frame")

    frames: list[Frame] = []
    for event in events:
        if isinstance(event, ControlChunk):
            frames.append(_normalize_frame(event.control, limits=active_limits))
        else:  # DataChunk
            frames.append(
                _normalize_frame(
                    {"type": data_frame_type, "data": event.data, "ts": time.time()},
                    limits=active_limits,
                )
            )
    return frames


def parse_frame(
    raw: str,
    *,
    data_frame_type: Literal["input", "term"] | None = None,
    limits: MessageLimits | None = None,
) -> Frame:
    if data_frame_type is None:
        frames = parse_stream(raw, data_frame_type="term", limits=limits)
        if len(frames) == 1 and frames[0].get("type") != "term":
            return frames[0]
        raise ProtocolError("data frame type required")
    frames = parse_stream(raw, data_frame_type=data_frame_type, limits=limits)
    if len(frames) != 1:
        raise ProtocolError("expected a single frame")
    return frames[0]


def frame_json(frame_type: FrameType, **kwargs: Any) -> str:
    payload = {"type": frame_type, "ts": time.time(), **kwargs}
    if frame_type in {"input", "term"}:
        return encode_data(str(payload.get("data", "")))
    return encode_control(payload)


# ---------------------------------------------------------------------------
# Runtime Protocol
#
# Structural interface implemented by SessionRuntime (CF DO) and the mock
# _Runtime used in tests.  Using a Protocol avoids importing the concrete DO
# class into the route modules, which would create circular imports and bring
# heavy CF-specific dependencies into unit tests.
# ---------------------------------------------------------------------------


class RuntimeProtocol(Protocol):
    worker_ws: CFWebSocket | None
    worker_id: str
    input_mode: str
    lifecycle_state: str
    meta: dict[str, Any]
    hijack: Any  # HijackCoordinator
    config: Any  # CloudflareConfig
    store: Any  # SqliteStateStore
    last_snapshot: Any
    last_analysis: Any
    browser_hijack_owner: dict[str, str]
    _ushell: Any  # UshellConnector | None
    _ushell_started: bool

    async def browser_role_for_request(self, request: object) -> str: ...
    async def browser_subject_for_request(self, request: object) -> str | None: ...
    async def request_json(self, request: object) -> dict[str, object]: ...
    def persist_lease(self, session: object) -> None: ...
    def clear_lease(self) -> None: ...
    async def push_worker_control(self, action: str, *, owner: str, lease_s: int) -> bool: ...
    async def broadcast_hijack_state(self) -> None: ...
    async def push_worker_input(self, data: str) -> bool: ...
    async def send_ws(self, ws: CFWebSocket, frame: dict[str, object]) -> None: ...
    async def broadcast_worker_frame(self, frame: object) -> None: ...
    def ws_key(self, ws: CFWebSocket) -> str: ...
    def _socket_browser_role(self, ws: CFWebSocket) -> str: ...
