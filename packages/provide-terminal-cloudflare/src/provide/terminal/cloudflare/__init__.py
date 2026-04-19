# SPDX-FileCopyrightText: Copyright (c) 2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("provide-terminal-cloudflare")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"  # pragma: no cover

from .config import CloudflareConfig

__all__ = ["CloudflareConfig", "__version__"]
