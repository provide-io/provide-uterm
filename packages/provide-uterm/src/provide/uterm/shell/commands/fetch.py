#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``fetch`` HTTP command — issues a request via the JS fetch API or urllib."""

from __future__ import annotations

from provide.uterm.shell._output import (
    GREEN,
    PROMPT,
    RESET,
    YELLOW,
    error_msg,
)


async def cmd_fetch(arg: str) -> list[str]:
    """Issue an HTTP request and return a formatted response preview."""
    if not arg:
        return [error_msg("usage: fetch [-X METHOD] <url> [body]") + PROMPT]

    method = "GET"
    rest = arg
    if rest == "-X" or rest.startswith(("-X ", "-X\t")):
        parts = rest[3:].lstrip().split(None, 1)
        if not parts:
            return [error_msg("usage: fetch [-X METHOD] <url> [body]") + PROMPT]
        method = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""

    url_body = rest.split(None, 1)
    url = url_body[0] if url_body else ""
    body = url_body[1] if len(url_body) > 1 else None

    if not url:
        return [error_msg("usage: fetch [-X METHOD] <url> [body]") + PROMPT]

    try:
        try:
            import js  # type: ignore[import-not-found]

            opts: dict[str, object] = {"method": method}
            if body is not None:
                opts["body"] = body
            resp = await js.fetch(url, opts)
            status = int(resp.status)
            text = await resp.text()
        except (ImportError, AttributeError):
            import urllib.request

            req = urllib.request.Request(url, method=method)  # noqa: S310  # nosec B310
            if body is not None:
                req.data = body.encode("utf-8")
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310  # nosec B310
                status = r.status
                text = r.read(4096).decode("utf-8", errors="replace")

        preview = text[:800].replace("\n", "\r\n")
        truncated = " …" if len(text) > 800 else ""
        color = GREEN if status < 400 else YELLOW if status < 500 else "\x1b[31m"
        return [f"{color}HTTP {status}{RESET}\r\n{preview}{truncated}\r\n" + PROMPT]
    except Exception as exc:
        return [error_msg(str(exc)) + PROMPT]
