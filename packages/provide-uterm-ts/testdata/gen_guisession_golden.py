#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for graphical sessions.

A graphical session is what an operator — or an agent — sees and touches on a
remote console, so two things are recorded here:

* **The framebuffer**, which refuses a size that is not a size and a pixel
  buffer that does not match the size it claims. A hostile ``ServerInit``
  announcing a screen of two billion pixels is the reason for the cap.
* **The PNG**, byte for byte. A screenshot is base64 in a JSON response and
  the client that decodes it is not this one, so a stream that differs by a
  single chunk is a screenshot that does not open.

Also recorded: what injecting a pointer does to the in-memory console, which
is the stub the ``memory`` protocol uses and the only console the tests have.

# uv-package: provide-uterm-server

Usage (from the repository root)::

    uv run --package provide-uterm-server python \\
        packages/provide-uterm-ts/testdata/gen_guisession_golden.py
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from provide.uterm.server import gui_session as gs

OUT = Path(__file__).resolve().parent / "guisession_golden.json"

# (name, width, height, pixel buffer or None)
IMAGES: list[tuple[str, int, int, list[int] | None]] = [
    ("one pixel", 1, 1, None),
    ("an ordinary console", 4, 3, None),
    ("the largest side there is", 8192, 1, None),
    ("one side too many", 8193, 1, None),
    ("the other side too many", 1, 8193, None),
    ("no width", 0, 1, None),
    ("no height", 1, 0, None),
    ("a negative side", -1, 1, None),
    ("pixels that fit", 2, 1, [1, 2, 3, 4, 5, 6, 7, 8]),
    ("pixels that are short", 2, 1, [1, 2, 3, 4]),
    ("pixels that are long", 2, 1, [1, 2, 3, 4, 5, 6, 7, 8, 9]),
    ("no pixels at all", 2, 1, []),
]

# (name, width, height, pixels) for the PNG encoder.
PNGS: list[tuple[str, int, int, list[int]]] = [
    ("one black pixel", 1, 1, [0, 0, 0, 255]),
    ("one white pixel", 1, 1, [255, 255, 255, 255]),
    ("one transparent pixel", 1, 1, [0, 0, 0, 0]),
    ("a row of two", 2, 1, [255, 0, 0, 255, 0, 0, 255, 255]),
    ("two rows", 1, 2, [255, 0, 0, 255, 0, 255, 0, 255]),
    ("a small square", 2, 2, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]),
    ("a run of one colour", 8, 8, [9, 8, 7, 255] * 64),
    ("every value in a row", 64, 1, [value % 256 for value in range(64 * 4)]),
    ("more pixels than it needs", 1, 1, [0, 0, 0, 255, 77, 77, 77, 77]),
    ("fewer pixels than it needs", 2, 2, [0, 0, 0, 255]),
    ("no width", 0, 1, []),
    ("no height", 1, 0, []),
    ("a negative side", -1, 1, []),
]

# (name, [(x, y, button_mask), ...]) against a 4x3 console.
POINTERS: list[tuple[str, list[tuple[int, int, int]]]] = [
    ("nothing at all", []),
    ("a press in the corner", [(0, 0, 1)]),
    ("a press in the far corner", [(3, 2, 1)]),
    ("a press in the middle", [(2, 1, 1)]),
    ("a release, which draws nothing", [(1, 1, 0)]),
    ("a press and then a release", [(1, 1, 1), (1, 1, 0)]),
    ("the other buttons, which draw nothing", [(1, 1, 2), (1, 1, 4)]),
    ("a button held with the first", [(1, 1, 3)]),
    ("one column past the edge", [(4, 0, 1)]),
    ("one row past the edge", [(0, 3, 1)]),
    ("a long way past the edge", [(9999, 9999, 1)]),
    ("before the first column", [(-1, 0, 1)]),
    ("before the first row", [(0, -1, 1)]),
    ("a drag across the console", [(0, 0, 1), (1, 1, 1), (2, 2, 1), (2, 2, 0)]),
]


def _error(call: Any) -> dict[str, Any]:
    try:
        value = call()
    except ValueError as exc:
        return {"error": str(exc)}
    return {"value": value}


def _image(width: int, height: int, pixels: list[int] | None) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        image = gs.RgbaImage(width, height, None if pixels is None else bytes(pixels))
        return {
            "width": image.width,
            "height": image.height,
            "pixels": base64.b64encode(bytes(image.pixels)).decode("ascii"),
        }

    return _error(build)


def _png(width: int, height: int, pixels: list[int]) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        encoded = gs.encode_rgba_png(width, height, bytes(pixels))
        return {
            "length": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "png": base64.b64encode(encoded).decode("ascii"),
        }

    return _error(build)


def _pointer(events: list[tuple[int, int, int]]) -> dict[str, Any]:
    session = gs.MemoryGraphicalSession(4, 3)
    for x, y, mask in events:
        session.inject_pointer(x, y, mask)
    # Keys do nothing to a memory console, and that it stays nothing is the
    # point: a stub that quietly drew would make a test pass for the wrong
    # reason.
    session.inject_key(0xFF0D, True)
    session.inject_key(0xFF0D, False)
    shot = session.screenshot()
    return {
        "width": shot.width,
        "height": shot.height,
        "pixels": base64.b64encode(bytes(shot.pixels)).decode("ascii"),
        "lit": [index // 4 for index in range(0, len(shot.pixels), 4) if shot.pixels[index] == 255],
    }


def main() -> None:
    default_session = gs.MemoryGraphicalSession()
    first = default_session.screenshot()
    first.pixels[0] = 200
    second = default_session.screenshot()

    corpus = {
        "max_dimension": gs.MAX_DIMENSION,
        "default_size": [default_session.screenshot().width, default_session.screenshot().height],
        # A screenshot that shared its buffer with the console would let a
        # caller paint on what everybody else is looking at.
        "screenshot_is_a_copy": second.pixels[0] == 0,
        "images": [
            {"name": name, "width": width, "height": height, "pixels": pixels, **_image(width, height, pixels)}
            for name, width, height, pixels in IMAGES
        ],
        "pngs": [
            {"name": name, "width": width, "height": height, **_png(width, height, pixels)}
            for name, width, height, pixels in PNGS
        ],
        "pointers": [
            {"name": name, "events": [list(event) for event in events], **_pointer(events)} for name, events in POINTERS
        ],
        # The 640x480 default is too large to write out, but its stream is
        # still the one a client has to open.
        "default_png": _png(640, 480, [0] * (640 * 480 * 4))["value"] | {"png": None},
    }
    corpus["default_png"].pop("png")
    OUT.write_text(json.dumps(corpus, indent=2) + "\n")
    print(f"wrote {OUT} ({len(corpus['pngs'])} streams)")


if __name__ == "__main__":
    main()
