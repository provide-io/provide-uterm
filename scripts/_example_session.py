"""Session state and worker simulation for example_server.py."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Literal

from provide.terminal.control_stream import (
    ControlStreamDecoder,
    ControlStreamProtocolError,
    DataChunk,
    encode_control,
    encode_data,
)
from provide.terminal.hijack.hub import TermHub

logger = logging.getLogger(__name__)

_SCREEN_COLS = 80
_SCREEN_ROWS = 25
_DEFAULT_PORT = 8742
_DEFAULT_WORKER_ID = "provide-shell"


@dataclass(slots=True)
class TranscriptEntry:
    speaker: Literal["system", "user", "session"]
    text: str
    ts: float


@dataclass(slots=True)
class DemoSessionState:
    worker_id: str
    title: str
    input_mode: Literal["hijack", "open"] = "hijack"
    paused: bool = False
    connected: bool = False
    turn_counter: int = 0
    transcript: list[TranscriptEntry] = field(default_factory=list)
    status_line: str = "Live"
    analysis_note: str | None = None
    last_snapshot_ts: float = 0.0
    pending_banner: str | None = "Ready. Type /help for commands."
    nickname: str = "user"
    last_command: str | None = None
    outbound_queue: asyncio.Queue[dict[str, Any]] | None = None


def _resolve_browser_role(_ws, _worker_id: str) -> str:
    """Demo server: grant full control so the widget is interactive by default."""
    return "admin"


_hub = TermHub(resolve_browser_role=_resolve_browser_role)
_sessions: dict[str, DemoSessionState] = {}
_sessions_lock = RLock()


def _new_session_state(worker_id: str) -> DemoSessionState:
    return DemoSessionState(
        worker_id=worker_id,
        title=f"Interactive Demo Session ({worker_id})",
        transcript=[
            TranscriptEntry("system", "Session online.", time.time()),
            TranscriptEntry("session", "Use /help, /mode open, /mode hijack, /clear, /reset.", time.time()),
        ],
    )


def _restore_session_defaults(session: DemoSessionState) -> None:
    """Reset mutable demo state in-place so worker loops keep a stable object reference."""
    connected = session.connected
    outbound_queue = session.outbound_queue
    worker_id = session.worker_id
    title = session.title
    replacement = _new_session_state(worker_id)
    session.title = title or replacement.title
    session.input_mode = replacement.input_mode
    session.paused = replacement.paused
    session.connected = connected
    session.turn_counter = replacement.turn_counter
    session.transcript = replacement.transcript
    session.status_line = replacement.status_line
    session.analysis_note = replacement.analysis_note
    session.last_snapshot_ts = replacement.last_snapshot_ts
    session.pending_banner = replacement.pending_banner
    session.nickname = replacement.nickname
    session.last_command = replacement.last_command
    session.outbound_queue = outbound_queue


def _get_or_create_session(worker_id: str) -> DemoSessionState:
    with _sessions_lock:
        session = _sessions.get(worker_id)
        if session is None:
            session = _new_session_state(worker_id)
            _sessions[worker_id] = session
        return session


def _reset_session_state(worker_id: str) -> DemoSessionState:
    with _sessions_lock:
        existing = _sessions.get(worker_id)
        if existing is None:
            existing = _new_session_state(worker_id)
            _sessions[worker_id] = existing
        else:
            _restore_session_defaults(existing)
        return existing


def _reset_all_sessions() -> None:
    with _sessions_lock:
        _sessions.clear()


def _session_payload(session: DemoSessionState) -> dict[str, Any]:
    return {
        "worker_id": session.worker_id,
        "title": session.title,
        "input_mode": session.input_mode,
        "paused": session.paused,
        "connected": session.connected,
        "turn_counter": session.turn_counter,
        "status_line": session.status_line,
        "analysis_note": session.analysis_note,
        "pending_banner": session.pending_banner,
        "transcript": [{"speaker": entry.speaker, "text": entry.text, "ts": entry.ts} for entry in session.transcript],
    }


def _append_entry(session: DemoSessionState, speaker: Literal["system", "user", "session"], text: str) -> None:
    session.transcript.append(TranscriptEntry(speaker, text, time.time()))
    session.transcript = session.transcript[-10:]


def _normalize_input(data: str) -> str:
    return data.replace("\r", "\n").replace("\t", " ").strip()


def _mode_label(session: DemoSessionState) -> str:
    return "Shared input" if session.input_mode == "open" else "Exclusive hijack"


def _control_label(session: DemoSessionState) -> str:
    return "Paused for hijack" if session.paused else "Live"


def _prompt_line(session: DemoSessionState) -> str:
    return f"{session.nickname}> "


def _render_screen(session: DemoSessionState) -> str:
    lines = [
        f"\x1b[1;36m[{session.title}]\x1b[0m",
        "-" * 60,
        f"\x1b[32mMode:\x1b[0m {_mode_label(session)}",
        f"\x1b[32mControl:\x1b[0m {_control_label(session)}",
        "\x1b[32mHelp:\x1b[0m /help /mode open|hijack /clear /nick /say /status /demo /reset",
    ]
    if session.pending_banner:
        lines.append(f"\x1b[33m{session.pending_banner}\x1b[0m")
    lines.append("")
    lines.append("\x1b[1mTranscript\x1b[0m")
    lines.extend(f"{entry.speaker:>7}: {entry.text}" for entry in session.transcript[-10:])
    lines.append("")
    lines.append(_prompt_line(session))
    visible = lines[-_SCREEN_ROWS:]
    return "\n".join(visible)


def _make_snapshot(session: DemoSessionState) -> dict[str, Any]:
    screen = _render_screen(session)
    session.last_snapshot_ts = time.time()
    visible_lines = screen.splitlines() or [""]
    cursor_y = min(len(visible_lines) - 1, _SCREEN_ROWS - 1)
    cursor_x = min(len(visible_lines[-1]), _SCREEN_COLS - 1)
    screen_hash = hashlib.sha256(screen.encode("utf-8")).hexdigest()[:16]
    return {
        "type": "snapshot",
        "screen": screen,
        "cursor": {"x": cursor_x, "y": cursor_y},
        "cols": _SCREEN_COLS,
        "rows": _SCREEN_ROWS,
        "screen_hash": screen_hash,
        "cursor_at_end": True,
        "has_trailing_space": False,
        "prompt_detected": {"prompt_id": "demo_prompt"},
        "ts": session.last_snapshot_ts,
    }


def _make_analysis(session: DemoSessionState) -> str:
    return "\n".join(
        [
            f"[interactive demo analysis — worker: {session.worker_id}]",
            f"input_mode: {session.input_mode}",
            f"paused: {session.paused}",
            f"turn_counter: {session.turn_counter}",
            f"transcript_entries: {len(session.transcript)}",
            f"last_command: {session.last_command or '(none)'}",
            f"status_line: {session.status_line}",
            f"prompt_visible: {_prompt_line(session).strip() != ''}",
            f"analysis_note: {session.analysis_note or '(none)'}",
        ]
    )


def _worker_hello(session: DemoSessionState) -> dict[str, Any]:
    return {"type": "worker_hello", "input_mode": session.input_mode, "ts": time.time()}


def _snapshot_only(session: DemoSessionState) -> list[dict[str, Any]]:
    return [_make_snapshot(session)]


def _state_update_messages(session: DemoSessionState) -> list[dict[str, Any]]:
    return [_worker_hello(session), _make_snapshot(session)]


def _set_input_mode(session: DemoSessionState, mode: str, *, source: str) -> list[dict[str, Any]]:
    if mode not in {"hijack", "open"}:
        raise ValueError(f"invalid mode: {mode}")
    session.input_mode = mode  # type: ignore[assignment]
    session.last_command = f"/mode {mode}"
    session.analysis_note = f"mode changed by {source}"
    session.pending_banner = f"Input mode set to {_mode_label(session)}."
    _append_entry(session, "system", f"mode: {_mode_label(session)} ({source})")
    return _state_update_messages(session)


def _status_summary(session: DemoSessionState) -> str:
    observer_count = 0
    with contextlib.suppress(Exception):
        st = _hub._workers.get(session.worker_id)
        if st is not None:
            observer_count = len(st.browsers)
    return f"mode={session.input_mode} paused={session.paused} turns={session.turn_counter} observers={observer_count}"


def _reset_via_command(session: DemoSessionState) -> list[dict[str, Any]]:
    _restore_session_defaults(session)
    session.last_command = "/reset"
    session.analysis_note = "reset from terminal command"
    session.pending_banner = "Session reset."
    return _state_update_messages(session)


def _apply_control(session: DemoSessionState, action: str) -> list[dict[str, Any]]:
    if action == "pause":
        session.paused = True
        session.status_line = "Paused for hijack"
        session.analysis_note = "exclusive control acquired"
        session.pending_banner = "Exclusive control active. Input is still accepted."
        _append_entry(session, "system", "control: hijack acquired")
    elif action == "resume":
        session.paused = False
        session.status_line = "Live"
        session.analysis_note = "control released"
        session.pending_banner = "Exclusive control released."
        _append_entry(session, "system", "control: released")
    elif action == "step":
        session.turn_counter += 1
        session.analysis_note = "step control processed"
        session.pending_banner = "Single-step acknowledged."
        _append_entry(session, "system", f"control: single step #{session.turn_counter}")
    else:
        session.pending_banner = f"Ignored unknown control action: {action}"
        _append_entry(session, "system", f"control: ignored {action}")
    return _snapshot_only(session)


def _apply_input(session: DemoSessionState, data: str) -> list[dict[str, Any]]:
    text = _normalize_input(data)
    if not text:
        session.pending_banner = "Empty input ignored."
        return _snapshot_only(session)

    session.turn_counter += 1
    if text.startswith("/"):
        cmd, _, rest = text.partition(" ")
        arg = rest.strip()
        session.last_command = cmd

        if cmd == "/help":
            session.analysis_note = "help requested"
            session.pending_banner = "Command help printed below."
            _append_entry(
                session,
                "system",
                "Commands: /help /clear /mode open|hijack /status /nick <name> /say <text> /demo /reset",
            )
            return _snapshot_only(session)

        if cmd == "/clear":
            session.transcript.clear()
            session.analysis_note = "transcript cleared"
            session.pending_banner = "Transcript cleared."
            return _snapshot_only(session)

        if cmd == "/mode":
            target = arg.lower()
            if target not in {"hijack", "open"}:
                session.pending_banner = "Usage: /mode open|hijack"
                _append_entry(session, "system", "usage: /mode open|hijack")
                return _snapshot_only(session)
            return _set_input_mode(session, target, source="terminal")

        if cmd == "/status":
            session.analysis_note = "status requested"
            session.pending_banner = "Session status printed below."
            _append_entry(session, "system", _status_summary(session))
            return _snapshot_only(session)

        if cmd == "/nick":
            if not arg:
                session.pending_banner = "Usage: /nick <name>"
                _append_entry(session, "system", "usage: /nick <name>")
                return _snapshot_only(session)
            session.nickname = arg[:24]
            session.analysis_note = f"nickname set to {session.nickname}"
            session.pending_banner = f"Nickname set to {session.nickname}."
            _append_entry(session, "system", f"nickname: {session.nickname}")
            return _snapshot_only(session)

        if cmd == "/say":
            if not arg:
                session.pending_banner = "Usage: /say <text>"
                _append_entry(session, "system", "usage: /say <text>")
                return _snapshot_only(session)
            _append_entry(session, "user", f"{session.nickname}: {arg}")
            session.analysis_note = "explicit message appended"
            session.pending_banner = "Message appended."
            return _snapshot_only(session)

        if cmd == "/demo":
            session.analysis_note = "demo command executed"
            session.pending_banner = "Demo response appended."
            _append_entry(session, "session", "This session accepts input while hijacked and in shared mode.")
            return _snapshot_only(session)

        if cmd == "/reset":
            return _reset_via_command(session)

        session.pending_banner = f"Unknown command: {cmd}"
        _append_entry(session, "system", f"unknown command: {cmd}")
        return _snapshot_only(session)

    _append_entry(session, "user", f"{session.nickname}: {text}")
    _append_entry(session, "session", f'session: received "{text}"')
    session.analysis_note = "free-form input received"
    session.pending_banner = "Input accepted."
    return _snapshot_only(session)


def _enqueue_worker_messages(session: DemoSessionState, messages: list[dict[str, Any]]) -> None:
    queue = session.outbound_queue
    if queue is None:
        return
    for msg in messages:
        queue.put_nowait(msg)


async def _sync_hub_input_mode(worker_id: str, mode: str) -> None:
    """Keep the hub's browser-facing input mode aligned with the demo worker."""
    ok, err = await _hub.set_input_mode(worker_id, mode)
    if not ok and err != "not_found":
        logger.debug("demo_sync_input_mode_failed worker_id=%s mode=%s err=%s", worker_id, mode, err)


async def _force_release_hijack_for_shared_mode(worker_id: str) -> bool:
    """Clear any active hijack so the demo can switch into shared-input mode immediately."""
    return await _hub.force_release_hijack(worker_id)


async def _run_session_worker(base_url: str, worker_id: str) -> None:
    """Continuously connect as a demo session worker, auto-reconnecting on failure."""
    import websockets

    def _encode_frame(payload: dict[str, Any]) -> str:
        if str(payload.get("type") or "") == "term":
            return encode_data(str(payload.get("data", "")))
        return encode_control(payload)

    ws_url = base_url.replace("http://", "ws://") + f"/ws/worker/{worker_id}/term"
    backoff_s = [0.5, 1.0, 2.0, 5.0, 10.0]
    attempt = 0
    session = _get_or_create_session(worker_id)

    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                attempt = 0
                session.connected = True
                session.outbound_queue = asyncio.Queue()
                decoder = ControlStreamDecoder()
                logger.info("demo_session_connected worker_id=%s", worker_id)

                await ws.send(_encode_frame(_worker_hello(session)))
                await ws.send(_encode_frame(_make_snapshot(session)))

                while True:
                    recv_task = asyncio.create_task(ws.recv())
                    queue_task = asyncio.create_task(session.outbound_queue.get())
                    done, pending = await asyncio.wait(
                        {recv_task, queue_task},
                        timeout=30.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    if not done:
                        await ws.send(_encode_frame(_make_snapshot(session)))
                        continue
                    if queue_task in done:
                        await ws.send(_encode_frame(queue_task.result()))
                        with contextlib.suppress(asyncio.CancelledError):
                            await recv_task
                        continue
                    raw = recv_task.result()
                    with contextlib.suppress(asyncio.CancelledError):
                        await queue_task

                    try:
                        events = decoder.feed(raw)
                    except ControlStreamProtocolError:
                        continue

                    for event in events:
                        if isinstance(event, DataChunk):
                            msg: dict[str, Any] = {"type": "input", "data": event.data}
                        else:
                            msg = event.control

                        mtype = msg.get("type")
                        if mtype == "snapshot_req":
                            await ws.send(_encode_frame(_make_snapshot(session)))
                        elif mtype == "analyze_req":
                            await ws.send(
                                _encode_frame(
                                    {
                                        "type": "analysis",
                                        "formatted": _make_analysis(session),
                                        "ts": time.time(),
                                    }
                                )
                            )
                        elif mtype == "control":
                            for outbound in _apply_control(session, str(msg.get("action", ""))):
                                await ws.send(_encode_frame(outbound))
                        elif mtype == "input":
                            raw_input = str(msg.get("data", ""))
                            normalized = _normalize_input(raw_input)
                            if normalized == "/mode open":
                                released = await _force_release_hijack_for_shared_mode(worker_id)
                                if released:
                                    session.paused = False
                                    session.status_line = "Live"
                                    _append_entry(session, "system", "control: released for shared input")
                            for outbound in _apply_input(session, raw_input):
                                await ws.send(_encode_frame(outbound))
                            if normalized in {"/mode open", "/mode hijack"}:
                                await _sync_hub_input_mode(worker_id, session.input_mode)
        except asyncio.CancelledError:
            logger.info("demo_session_cancelled worker_id=%s", worker_id)
            session.connected = False
            session.outbound_queue = None
            return
        except Exception as exc:
            session.connected = False
            session.outbound_queue = None
            delay = backoff_s[min(attempt, len(backoff_s) - 1)]
            logger.debug(
                "demo_session_disconnected worker_id=%s attempt=%d delay=%.1fs: %s",
                worker_id,
                attempt,
                delay,
                exc,
            )
            attempt += 1
            await asyncio.sleep(delay)


def _start_default_session_workers(base_url: str) -> list[asyncio.Task[None]]:
    return [asyncio.create_task(_run_session_worker(base_url, _DEFAULT_WORKER_ID))]
