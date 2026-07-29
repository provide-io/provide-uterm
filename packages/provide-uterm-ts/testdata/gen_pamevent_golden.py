#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Generate the differential golden corpus for PAM notification events.

A PAM notify event arrives as one JSON line on a Unix socket and can start a
root-side session, so what this parser refuses is the security boundary:

* **Anything it does not understand becomes nothing.** A line that is not
  JSON, names an event outside ``open``/``close``, or carries no username is
  dropped and the connection stays up — one bad line from a confused sender
  must not end a listener that other logins depend on.
* **The mode is narrowed, not trusted.** Anything that is not exactly
  ``capture`` is ``notify``, so a sender cannot reach the capture path by
  spelling it differently.
* **A pid that is not a number becomes zero** rather than failing the event,
  because the pid is advisory and the username is not.

The line-length cap and the peer-uid rule are recorded with it: an oversized
line is dropped without dropping the connection, and a peer whose uid cannot
be determined is *allowed* — the socket's 0600 mode is the baseline, and
refusing everything on a platform with no ``SO_PEERCRED`` would mean no
sessions at all there.

# uv-package: provide-uterm-platform

Usage (from the repository root)::

    uv run --package provide-uterm-platform python \\
        packages/provide-uterm-ts/testdata/gen_pamevent_golden.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from provide.uterm.pty import pam_listener

OUT = Path(__file__).resolve().parent / "pamevent_golden.json"


def _parse(line: bytes) -> dict[str, Any] | None:
    """What the reference does with this line.

    ``_parse_event`` documents itself as returning ``None`` on any error, and
    for a line that is valid JSON but not an *object* that is not what happens:
    ``data.get`` is called on a list, a string or a null and raises. The
    exception propagates out of the connection handler, so one such line from
    any sender past the socket's permission gate ends that connection and logs
    a traceback. Recorded rather than fixed here — the port's own behaviour is
    a deliberate divergence, noted in its roadmap.
    """
    try:
        event = pam_listener._parse_event(line)
    except Exception as exc:
        return {"raises": type(exc).__name__}
    if event is None:
        return None
    return {
        "event": event.event,
        "username": event.username,
        "tty": event.tty,
        "pid": event.pid,
        "mode": event.mode,
        "capture_socket": event.capture_socket,
    }


# Written as bytes because that is what comes off the socket, and because some
# of these are not valid text at all.
LINES: list[tuple[str, bytes]] = [
    ("an ordinary login", b'{"event":"open","username":"ada","tty":"pts/3","pid":4242}'),
    ("an ordinary logout", b'{"event":"close","username":"ada","tty":"pts/3","pid":4242}'),
    ("a line with a trailing newline", b'{"event":"open","username":"ada"}\n'),
    ("a line with spaces around it", b'  {"event":"open","username":"ada"}  \n'),
    ("nothing at all", b""),
    ("a line that is not JSON", b"open ada pts/3"),
    ("a line that is half JSON", b'{"event":"open","username":'),
    ("a JSON list rather than an object", b'["open","ada"]'),
    ("a JSON string rather than an object", b'"open"'),
    ("a JSON null", b"null"),
    ("an event nobody defined", b'{"event":"hijack","username":"ada"}'),
    ("no event at all", b'{"username":"ada"}'),
    ("an event that is not a string", b'{"event":1,"username":"ada"}'),
    ("no username", b'{"event":"open"}'),
    ("an empty username", b'{"event":"open","username":""}'),
    ("a username given null", b'{"event":"open","username":null}'),
    ("a username that is a number", b'{"event":"open","username":1000}'),
    ("no tty", b'{"event":"open","username":"ada"}'),
    ("a tty given null", b'{"event":"open","username":"ada","tty":null}'),
    ("a pid given as a string", b'{"event":"open","username":"ada","pid":"4242"}'),
    ("a pid that is not a number", b'{"event":"open","username":"ada","pid":"soon"}'),
    ("a pid given null", b'{"event":"open","username":"ada","pid":null}'),
    ("a pid given a list", b'{"event":"open","username":"ada","pid":[1]}'),
    ("a negative pid", b'{"event":"open","username":"ada","pid":-1}'),
    ("the capture mode", b'{"event":"open","username":"ada","mode":"capture"}'),
    ("a mode nobody defined", b'{"event":"open","username":"ada","mode":"record"}'),
    ("the capture mode in capitals", b'{"event":"open","username":"ada","mode":"CAPTURE"}'),
    ("a mode given null", b'{"event":"open","username":"ada","mode":null}'),
    ("a capture socket", b'{"event":"open","username":"ada","mode":"capture","capture_socket":"/run/c.sock"}'),
    ("a capture socket that is empty", b'{"event":"open","username":"ada","capture_socket":""}'),
    ("a capture socket given null", b'{"event":"open","username":"ada","capture_socket":null}'),
    ("a capture socket that is a number", b'{"event":"open","username":"ada","capture_socket":5}'),
    ("a username with a null byte in it", b'{"event":"open","username":"ada\\u0000root"}'),
    ("bytes that are not valid UTF-8", b'{"event":"open","username":"ad\xffa"}'),
    ("a name nobody defined alongside the rest", b'{"event":"open","username":"ada","colour":"green"}'),
]


def main() -> None:
    corpus = {
        "max_line": pam_listener._MAX_LINE,
        "socket_mode": pam_listener._NOTIFY_SOCKET_MODE,
        "bind_umask": pam_listener._NOTIFY_BIND_UMASK,
        "lines": [
            {
                "name": name,
                # Recorded as latin-1 text, which is byte-for-character, so a
                # line that is not valid UTF-8 survives the round trip.
                "line": line.decode("latin-1"),
                "event": _parse(line),
            }
            for name, line in LINES
        ],
    }
    OUT.write_text(json.dumps(corpus, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} ({len(corpus['lines'])} lines)")


if __name__ == "__main__":
    main()
