#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

try:
    from provide.uterm.cloudflare.api.http_routes._dispatch import route_http
except Exception:  # pragma: no cover
    from api.http_routes._dispatch import (  # type: ignore[import-not-found,no-redef]  # ty:ignore[unresolved-import]
        route_http,  # CF flat path  # pragma: no cover
    )

__all__ = ["route_http"]
