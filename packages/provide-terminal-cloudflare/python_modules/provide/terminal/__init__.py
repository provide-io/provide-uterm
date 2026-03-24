#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""provide-terminal: shared terminal I/O primitives for the provide ecosystem."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("provide-terminal")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

from provide.terminal.ansi import (
    BOLD,
    CLEAR_SCREEN,
    DEFAULT_PALETTE,
    DEFAULT_RGB,
    RESET,
    normalize_colors,
    preview_ansi,
    register_color_dialect,
    registered_dialects,
    unregister_color_dialect,
    upgrade_to_256,
    upgrade_to_truecolor,
)
from provide.terminal.file_io import load_ans, load_palette, load_txt
from provide.terminal.line_editor import LineEditor
from provide.terminal.screen import (
    clean_screen_for_display,
    decode_cp437,
    encode_cp437,
    extract_action_tags,
    extract_key_value_pairs,
    extract_menu_options,
    extract_numbered_list,
    normalize_terminal_text,
    strip_ansi,
)

__all__ = [
    "__version__",
    # ansi
    "CLEAR_SCREEN",
    "BOLD",
    "RESET",
    "DEFAULT_PALETTE",
    "DEFAULT_RGB",
    "normalize_colors",
    "preview_ansi",
    "register_color_dialect",
    "unregister_color_dialect",
    "registered_dialects",
    "upgrade_to_256",
    "upgrade_to_truecolor",
    # file_io
    "load_ans",
    "load_txt",
    "load_palette",
    # line_editor
    "LineEditor",
    # screen
    "strip_ansi",
    "normalize_terminal_text",
    "decode_cp437",
    "encode_cp437",
    "extract_action_tags",
    "clean_screen_for_display",
    "extract_menu_options",
    "extract_numbered_list",
    "extract_key_value_pairs",
    # fastapi (optional — requires [websocket] extra; excluded from import *)
    # "mount_terminal_ui",  -- intentionally omitted: raises ImportError without fastapi
]


_FASTAPI_EXPORTS: frozenset[str] = frozenset({"mount_terminal_ui", "create_ws_terminal_router", "WsTerminalProxy"})
_GATEWAY_EXPORTS: frozenset[str] = frozenset({"TelnetWsGateway", "SshWsGateway"})
_SERVER_EXPORTS: frozenset[str] = frozenset({"create_server_app", "load_server_config", "default_server_config"})


def __getattr__(name: str) -> object:
    if name in _FASTAPI_EXPORTS:
        import provide.terminal.fastapi as _fastapi_mod

        return getattr(_fastapi_mod, name)
    if name in _GATEWAY_EXPORTS:
        import provide.terminal.gateway as _gateway_mod

        return getattr(_gateway_mod, name)
    if name in _SERVER_EXPORTS:
        import provide.terminal.server as _server_mod

        return getattr(_server_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
