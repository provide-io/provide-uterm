#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""RFB (VNC) client behind ``gui/attach`` — a real graphical session.

Python port of the C# canonical
(``packages/provide-uterm-csharp/src/Provide.Uterm/Vnc/RfbClient.cs``), which is
the reference for the graphical stack.

Until this existed, :func:`~provide.uterm.server.bridge.routes.rest_gui` refused
every protocol but ``memory`` with 501 and the whole ``gui_*`` tool surface —
screenshot, click, drag, type, key — could only reach
:class:`MemoryGraphicalSession`, whose framebuffer is a stub that paints one
white pixel per click and ignores keys entirely.

What this does NOT re-implement: the dial. :func:`open_rfb_upstream` already
does TCP with opt-in TLS and fail-closed certificate verification, and the human
relay has used it in production. This is the protocol layer on top of those two
streams — handshake, framebuffer tracking, input encoding.

Scope matches the C# reference deliberately, so the ports stay differentially
comparable: security type ``None`` only, ``Raw`` and ``CopyRect`` encodings.
A server that offers neither ``None`` nor a usable pixel format is refused at
connect rather than half-supported.
"""

from __future__ import annotations

import struct
import threading
from typing import TYPE_CHECKING, BinaryIO

from provide.telemetry import get_logger
from provide.uterm.server.gui_session import MAX_DIMENSION, RgbaImage
from provide.uterm.server.vnc_upstream import dial_config_from_target, open_rfb_upstream

if TYPE_CHECKING:
    from provide.uterm.server.graphical_targets import GraphicalTargetDefinition

logger = get_logger(__name__)

#: RFB encodings this client asks for, in preference order.
ENCODING_RAW = 0
ENCODING_COPY_RECT = 1

#: The only security type supported, matching the C# reference.
SECURITY_NONE = 1

#: Server→client message types.
_MSG_FRAMEBUFFER_UPDATE = 0

#: Refuse absurd values from a hostile or broken server before allocating.
_MAX_RECTS = 4096
_MAX_DESKTOP_NAME = 4096


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    """Read exactly *count* bytes or raise; short reads are a protocol error."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise ConnectionError(f"RFB stream closed with {remaining} of {count} bytes outstanding")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _negotiate_version(reader: BinaryIO, writer: BinaryIO) -> str:
    """Agree a ProtocolVersion, preferring 3.8 when the server offers it."""
    server_version = _read_exact(reader, 12).decode("ascii", errors="replace")
    if not server_version.startswith("RFB "):
        # Not an RFB peer at all. Say so here rather than failing later on a
        # nonsense security byte.
        raise ConnectionError(f"not an RFB server: {server_version!r}")
    client_version = "RFB 003.008\n" if "003.008" in server_version else server_version
    writer.write(client_version.encode("ascii"))
    return client_version


def _negotiate_security(reader: BinaryIO, writer: BinaryIO, client_version: str) -> None:
    """Complete the security handshake, which must land on type ``None``."""
    if "003.007" in client_version or "003.008" in client_version:
        count = _read_exact(reader, 1)[0]
        if count == 0:
            # 3.7+ signals failure with an empty list plus a reason string.
            raise ConnectionError("RFB security handshake failed (server offered no types)")
        offered = _read_exact(reader, count)
        if SECURITY_NONE not in offered:
            raise ConnectionError(f"RFB server does not offer security type None (offered {sorted(offered)})")
        writer.write(bytes([SECURITY_NONE]))
        # SecurityResult is 3.8 only; 3.7 proceeds straight to ClientInit.
        if "003.008" in client_version:
            (result,) = struct.unpack(">I", _read_exact(reader, 4))
            if result != 0:
                raise ConnectionError("RFB security rejected")
        return

    # 3.3: the server dictates a single type as a u32.
    (security_type,) = struct.unpack(">I", _read_exact(reader, 4))
    if security_type != SECURITY_NONE:
        raise ConnectionError(f"unsupported RFB security type {security_type}")


def _read_server_init(reader: BinaryIO) -> tuple[int, int]:
    """Read ServerInit and return validated ``(width, height)``."""
    header = _read_exact(reader, 24)
    width, height = struct.unpack(">HH", header[0:4])
    if width == 0 or height == 0 or width > MAX_DIMENSION or height > MAX_DIMENSION:
        # Same cap MemoryGraphicalSession enforces: a hostile ServerInit must
        # not make us allocate on the strength of a remote number.
        raise ConnectionError(f"RFB framebuffer dimensions out of range: {width}x{height}")
    (name_len,) = struct.unpack(">I", header[20:24])
    if name_len > _MAX_DESKTOP_NAME:
        raise ConnectionError("RFB desktop name too long")
    if name_len:
        _read_exact(reader, name_len)
    return width, height


def _send_set_pixel_format(writer: BinaryIO) -> None:
    """Ask for 32bpp true-colour BGRA, which is what the tracker assumes."""
    writer.write(
        struct.pack(
            ">BBBB BBBB HHH BBB BBB",
            0,  # SetPixelFormat
            0,
            0,
            0,  # padding
            32,  # bits-per-pixel
            24,  # depth
            0,  # big-endian-flag
            1,  # true-colour-flag
            255,  # red-max
            255,  # green-max
            255,  # blue-max
            16,  # red-shift
            8,  # green-shift
            0,  # blue-shift
            0,
            0,
            0,  # padding
        )
    )


def _send_set_encodings(writer: BinaryIO) -> None:
    writer.write(struct.pack(">BBHii", 2, 0, 2, ENCODING_RAW, ENCODING_COPY_RECT))


def _send_update_request(writer: BinaryIO, width: int, height: int, *, incremental: bool) -> None:
    writer.write(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0, width, height))


def encode_pointer_event(x: int, y: int, button_mask: int) -> bytes:
    """PointerEvent (message type 5)."""
    return struct.pack(">BBHH", 5, button_mask & 0xFF, max(0, x), max(0, y))


def encode_key_event(key_sym: int, *, down: bool) -> bytes:
    """KeyEvent (message type 4)."""
    return struct.pack(">BBHI", 4, 1 if down else 0, 0, key_sym)


class RfbGraphicalSession:
    """A live RFB connection presented as a :class:`GraphicalSession`.

    The framebuffer is maintained by a daemon reader thread so ``screenshot()``
    is a cheap copy of already-received state rather than a round trip — which
    is what makes the pull-based ``gui_screenshot`` tool responsive even when
    the upstream is sending sparse updates.
    """

    __slots__ = ("_closed", "_fb", "_lock", "_reader", "_thread", "_write_lock", "_writer", "height", "width")

    def __init__(self, reader: BinaryIO, writer: BinaryIO, width: int, height: int) -> None:
        self._reader = reader
        self._writer = writer
        self.width = width
        self.height = height
        self._fb = RgbaImage(width, height)
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, name="rfb-reader", daemon=True)
        self._thread.start()

    @classmethod
    def connect(cls, target: GraphicalTargetDefinition) -> RfbGraphicalSession:
        """Dial *target* and complete the handshake, or raise."""
        dial = dial_config_from_target(target)
        if dial is None:
            raise ConnectionError(f"target {target.target_id!r} is not a dialable rfb target")
        reader, writer = open_rfb_upstream(dial)
        try:
            client_version = _negotiate_version(reader, writer)
            _negotiate_security(reader, writer, client_version)
            writer.write(bytes([1]))  # ClientInit, shared = 1
            width, height = _read_server_init(reader)
            _send_set_pixel_format(writer)
            _send_set_encodings(writer)
            _send_update_request(writer, width, height, incremental=False)
        except Exception:
            reader.close()
            writer.close()
            raise
        logger.info("rfb_session_connected target=%s size=%dx%d", target.target_id, width, height)
        return cls(reader, writer, width, height)

    def _read_loop(self) -> None:
        """Apply framebuffer updates until the peer closes or we do."""
        try:
            while not self._closed.is_set():
                message_type = _read_exact(self._reader, 1)[0]
                if message_type != _MSG_FRAMEBUFFER_UPDATE:
                    # ServerCutText / Bell / SetColourMapEntries: not tracked,
                    # and skipping them blindly would desynchronise the stream,
                    # so stop rather than guess at a length.
                    logger.debug("rfb_unhandled_message type=%d", message_type)
                    return
                _, rect_count = struct.unpack(">BH", _read_exact(self._reader, 3))
                if rect_count > _MAX_RECTS:
                    raise ConnectionError(f"RFB rectangle count too large: {rect_count}")
                for _ in range(rect_count):
                    self._apply_rect()
                with self._write_lock:
                    _send_update_request(self._writer, self.width, self.height, incremental=True)
        except Exception as exc:
            if not self._closed.is_set():
                logger.info("rfb_read_loop_ended error=%s", exc)
        finally:
            self._closed.set()

    def _apply_rect(self) -> None:
        """Read one rectangle header and its payload."""
        x, y, w, h, encoding = struct.unpack(">HHHHi", _read_exact(self._reader, 12))
        if w == 0 or h == 0:
            return
        if x + w > self.width or y + h > self.height:
            raise ConnectionError(f"RFB rect out of bounds: {x},{y} {w}x{h}")
        if encoding == ENCODING_RAW:
            pixels = _read_exact(self._reader, w * h * 4)
            with self._lock:
                self._blit(x, y, w, h, pixels)
        elif encoding == ENCODING_COPY_RECT:
            # Consume the source coordinates. The C# reference tracks these
            # best-effort too; a missed copyrect costs staleness in a region,
            # not a desynchronised stream.
            _read_exact(self._reader, 4)
        else:
            raise ConnectionError(f"RFB encoding not negotiated: {encoding}")

    def _blit(self, x: int, y: int, w: int, h: int, pixels: bytes) -> None:
        """Copy a BGRA rectangle into the RGBA framebuffer. Caller holds the lock."""
        fb = self._fb.pixels
        stride = self.width * 4
        for row in range(h):
            src = row * w * 4
            dst = ((y + row) * stride) + (x * 4)
            for col in range(w):
                s = src + (col * 4)
                d = dst + (col * 4)
                # Wire order is BGRA under the pixel format we requested.
                fb[d] = pixels[s + 2]
                fb[d + 1] = pixels[s + 1]
                fb[d + 2] = pixels[s]
                fb[d + 3] = 255

    def screenshot(self) -> RgbaImage:
        with self._lock:
            return self._fb.clone()

    def inject_pointer(self, x: int, y: int, button_mask: int) -> None:
        self._send(encode_pointer_event(x, y, button_mask))

    def inject_key(self, key_sym: int, down: bool) -> None:
        self._send(encode_key_event(key_sym, down=down))

    def _send(self, payload: bytes) -> None:
        if self._closed.is_set():
            raise ConnectionError("RFB session is closed")
        with self._write_lock:
            self._writer.write(payload)

    def close(self) -> None:
        """Stop the reader and release both stream ends."""
        self._closed.set()
        for stream in (self._reader, self._writer):
            try:
                stream.close()
            except OSError:
                logger.debug("rfb_close_error", exc_info=True)
