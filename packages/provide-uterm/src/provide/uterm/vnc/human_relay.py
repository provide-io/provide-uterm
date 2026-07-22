#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Bidirectional human VNC stream relay (browser ↔ upstream RFB).

Pumps upstream→browser as raw bytes and browser→upstream through
:func:`filter_rfb_client_input` so KeyEvent / PointerEvent / ClientCutText
are gated on ``can_inject``. A missing inject callback fails closed (drops
inject messages) — same semantics as the Go ``ServeHumanRelay`` path.
"""

from __future__ import annotations

import io
import threading
from typing import TYPE_CHECKING, BinaryIO

from provide.telemetry import get_logger
from provide.uterm.vnc.rfb_filter import CanInjectFn, filter_rfb_client_input

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_PUMP_CHUNK = 65_536
_JOIN_TIMEOUT_S = 5.0


def _unblock_fd_stream(stream: BinaryIO) -> None:
    """Close *stream* only when it has a real FD (socket/pipe), not BytesIO."""
    try:
        stream.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        return
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            pass


def run_human_relay_streams(
    browser_r: BinaryIO,
    browser_w: BinaryIO,
    upstream_r: BinaryIO,
    upstream_w: BinaryIO,
    *,
    can_inject: CanInjectFn | None,
    session_id: str,
    lease_id: str,
    principal_id: str,
    principal_role: str,
    on_upstream_eof: Callable[[], None] | None = None,
) -> None:
    """Relay RFB between browser and upstream streams until either side EOFs.

    * Upstream → browser: chunked byte pump with flush-per-write (live RFB
      peers send data without EOF; buffered makefiles must be opened with
      ``buffering=0`` at the socket boundary).
    * Browser → upstream: :func:`filter_rfb_client_input` (handshake pass-through;
      inject types gated). ``can_inject is None`` fails closed.

    Runs the upstream pump on a daemon thread so both directions progress
    concurrently (socketpair / live sockets). Safe with sequential ``BytesIO``
    fixtures when each side is fully pre-buffered.

    *on_upstream_eof*, if given, is invoked (from the pump thread) once the
    upstream side is fully drained and EOFs/errors. The owner uses it to tear
    down the browser side — otherwise the browser→upstream filter stays parked
    reading an idle browser forever, hanging the relay after the VNC server has
    already gone away.
    """
    pump_errors: list[BaseException] = []

    def _pump_upstream() -> None:
        # Chunked read + flush-per-write: live RFB peers send ProtocolVersion
        # then wait (no EOF). shutil.copyfileobj alone can leave makefile
        # write buffers unflushed until the upstream closes, so the browser
        # never sees the banner. Flush after every successful write.
        try:
            while True:
                chunk = upstream_r.read(_PUMP_CHUNK)
                if not chunk:
                    break
                browser_w.write(chunk)
                flush = getattr(browser_w, "flush", None)
                if callable(flush):
                    flush()
        except BaseException as exc:
            pump_errors.append(exc)
            logger.debug("vnc_upstream_pump_error error=%s", exc)
        finally:
            # Upstream is done (EOF or error): the relay session is over. Signal
            # the owner so it can tear down the browser side, which is otherwise
            # parked reading browser input forever when the browser sits idle.
            # Fires only after the pump has drained all upstream bytes.
            if on_upstream_eof is not None:
                try:
                    on_upstream_eof()
                except Exception as cb_exc:
                    # Callback must never kill the pump thread.
                    logger.debug("vnc_upstream_eof_callback_error error=%s", cb_exc)

    pump = threading.Thread(target=_pump_upstream, name="vnc-human-relay-upstream", daemon=True)
    pump.start()
    try:
        filter_rfb_client_input(
            upstream_w,
            browser_r,
            can_inject=can_inject,
            session_id=session_id,
            lease_id=lease_id,
            principal_id=principal_id,
            principal_role=principal_role,
        )
        flush_up = getattr(upstream_w, "flush", None)
        if callable(flush_up):
            flush_up()
    finally:
        # Unblock a stuck FD-backed pump without clobbering BytesIO fixtures.
        _unblock_fd_stream(upstream_r)
        _unblock_fd_stream(browser_w)
        pump.join(timeout=_JOIN_TIMEOUT_S)

    # Upstream pump errors are almost always shutdown races (closed pipe).
    # Log them; the filter path is the authoritative result.
    for exc in pump_errors:
        if isinstance(exc, (OSError, ValueError)):
            logger.debug("vnc_upstream_pump_shutdown error=%s", exc)
            continue
        raise exc
