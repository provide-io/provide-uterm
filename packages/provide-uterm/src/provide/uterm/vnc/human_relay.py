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
import struct
import threading
from typing import TYPE_CHECKING, BinaryIO

from provide.telemetry import get_logger
from provide.uterm.vnc.rfb_filter import CanInjectFn, filter_rfb_client_input

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_PUMP_CHUNK = 65_536
_JOIN_TIMEOUT_S = 5.0

# Default cadence for the update-driver (see run_human_relay_streams'
# drive_update_interval_s). ~25 requests/s keeps motion smooth without flooding
# the upstream; x11vnc coalesces to actual damage so idle screens stay cheap.
DEFAULT_UPDATE_DRIVE_INTERVAL_S = 0.04
# Incremental FramebufferUpdateRequest for the whole surface (u16 max w/h; the
# RFB server clamps to the real framebuffer). Injected to keep an animating
# upstream streaming to clients (e.g. noVNC) that only ever send one full request.
_DRIVE_FBUR = struct.pack(">BBHHHH", 3, 1, 0, 0, 0xFFFF, 0xFFFF)
_DRIVE_HANDSHAKE_WAIT_S = 10.0


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
    drive_update_interval_s: float | None = None,
) -> None:
    """Relay RFB between browser and upstream streams until either side EOFs.

    * Upstream → browser: chunked byte pump with flush-per-write (live RFB
      peers send data without EOF; buffered makefiles must be opened with
      ``buffering=0`` at the socket boundary).
    * Browser → upstream: :func:`filter_rfb_client_input` (handshake pass-through;
      inject types gated). ``can_inject is None`` fails closed.

    *drive_update_interval_s*, if set (> 0), starts an update-driver thread that
    injects an incremental ``FramebufferUpdateRequest`` upstream every interval
    once the handshake completes. This keeps an animating upstream streaming to
    clients that request only one full update and then go silent (noVNC does
    exactly this — without the driver the mirror freezes on frame 1). The driver
    and the browser→upstream filter share a write lock so their writes to
    ``upstream_w`` never interleave mid-message.

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

    write_lock = threading.Lock()
    handshake_done = threading.Event()
    stop_driver = threading.Event()
    drive = drive_update_interval_s is not None and drive_update_interval_s > 0

    def _drive_updates(interval: float) -> None:
        # Wait for the handshake (ClientInit forwarded) so the injected requests
        # land after ServerInit; then poll the upstream for changes on a timer.
        if not handshake_done.wait(timeout=_DRIVE_HANDSHAKE_WAIT_S):
            return
        while not stop_driver.is_set():
            try:
                # upstream_w is unbuffered (buffering=0), so the write lands
                # without an explicit flush — same contract the filter relies on.
                with write_lock:
                    upstream_w.write(_DRIVE_FBUR)
            except BaseException as exc:  # upstream closed / shutdown race
                logger.debug("vnc_update_driver_stopped error=%s", exc)
                return
            stop_driver.wait(interval)

    pump = threading.Thread(target=_pump_upstream, name="vnc-human-relay-upstream", daemon=True)
    pump.start()
    driver: threading.Thread | None = None
    if drive:
        driver = threading.Thread(
            target=_drive_updates,
            args=(float(drive_update_interval_s),),  # type: ignore[arg-type]
            name="vnc-human-relay-driver",
            daemon=True,
        )
        driver.start()
    try:
        filter_rfb_client_input(
            upstream_w,
            browser_r,
            can_inject=can_inject,
            session_id=session_id,
            lease_id=lease_id,
            principal_id=principal_id,
            principal_role=principal_role,
            dst_lock=write_lock,
            on_handshake_done=handshake_done.set,
        )
        with write_lock:
            flush_up = getattr(upstream_w, "flush", None)
            if callable(flush_up):
                flush_up()
    finally:
        # Stop the driver first so it can't write to a stream we're tearing down.
        stop_driver.set()
        if driver is not None:
            driver.join(timeout=_JOIN_TIMEOUT_S)
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
