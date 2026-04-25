#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""provide-terminal: shared terminal I/O primitives for the provide ecosystem.

Server, gateway, bridge, manager, and telnet session APIs live in split
packages and should be imported from their explicit submodules.
"""

from __future__ import annotations

import pkgutil

# Allow other installed packages to contribute sub-packages under provide.terminal
# (e.g. provide-terminal-cloudflare contributes provide.terminal.cloudflare).
__path__ = pkgutil.extend_path(__path__, __name__)

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
from provide.terminal.auth import (
    AuthorizedKeysFileResolver,
    NullResolver,
    ResolvedIdentity,
    SSHKeyResolver,
    fingerprint_from_openssh_blob,
)
from provide.terminal.colors import (
    apply_color_mode,
    downgrade_to_16,
    downgrade_to_256,
    rgb_to_16_index,
    rgb_to_256,
)
from provide.terminal.control_channel_builders import (
    make_identity,
    make_link_patterns,
    make_presence_update,
    make_resume,
    make_resume_failed,
    make_resume_ok,
    make_session_token,
)
from provide.terminal.control_channel_patterns import LinkPattern, LinkPatternRegistry
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
    # colors — downgrade counterparts
    "apply_color_mode",
    "downgrade_to_16",
    "downgrade_to_256",
    "rgb_to_16_index",
    "rgb_to_256",
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
    # control_channel_builders — typed builders for ControlChannel protocol messages
    "make_identity",
    "make_session_token",
    "make_resume",
    "make_resume_ok",
    "make_resume_failed",
    "make_link_patterns",
    "make_presence_update",
    # control_channel_patterns — server-side link_patterns registry
    "LinkPattern",
    "LinkPatternRegistry",
    # auth — pluggable SSH pubkey → identity
    "AuthorizedKeysFileResolver",
    "NullResolver",
    "ResolvedIdentity",
    "SSHKeyResolver",
    "fingerprint_from_openssh_blob",
    # control-plane namespaces
    "control_channel_namespace",
    "control_plane_namespace",
]

from provide.terminal.control import channel as control_channel_namespace
from provide.terminal.control import plane as control_plane_namespace
