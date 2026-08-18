#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""The single place this package constructs outbound HTTP clients.

Every outbound call in provide-uterm-server -- webhook delivery, delegated IdP
resolution, policy decisions, node discovery, recording upload, PAM
integration -- goes through :func:`async_client` rather than instantiating a
client inline. Two reasons, one practical and one structural:

**Configuration has one home.** Timeouts, connection limits, proxy and TLS
settings, and any future retry/egress policy are set here instead of being
re-derived at a dozen call sites.

**Tests get a seam.** Client construction used to happen inside the methods
under test (``async with httpx2.AsyncClient(...)`` in the middle of
``resolve_principal``), which left no way to substitute a transport. That is
why the suite reached for respx, which patches the HTTP stack process-wide.
Routing through this factory lets a test swap in a mock transport by patching
one function -- see ``tests/helpers/http_mock.py`` -- with no global patching
and no dependency on respx's internals.

Callers should not import the underlying HTTP library directly.
"""

from __future__ import annotations

from typing import Any

import httpx2

__all__ = ["AsyncClient", "HTTPError", "Response", "async_client"]

# Re-exported so call sites can annotate and catch without importing the HTTP
# library themselves. Keeping the import in one module is what makes swapping
# the implementation a single-file change rather than a repo-wide rename.
AsyncClient = httpx2.AsyncClient
Response = httpx2.Response
HTTPError = httpx2.HTTPError


def async_client(*, timeout: float | None = None, **kwargs: Any) -> httpx2.AsyncClient:
    """Build the package's outbound async HTTP client.

    Args:
        timeout: Total request timeout in seconds. ``None`` uses the library
            default rather than blocking forever.
        **kwargs: Passed through to the underlying client unchanged. Callers
            needing per-request headers pass them to the request method; this
            factory deliberately exposes no header defaults, because nothing
            in the package wants them and an unused knob is one more thing to
            keep covered.

    Returns:
        An unopened async client. Construction opens no sockets -- the
        connection is established lazily on the first request -- so callers may
        build one per call or hold a long-lived instance and ``aclose()`` it.
    """
    if timeout is not None:
        kwargs["timeout"] = timeout
    return httpx2.AsyncClient(**kwargs)
