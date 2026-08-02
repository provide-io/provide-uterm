#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""provide-uterm: shared terminal I/O primitives for the provide ecosystem.

Server, gateway, bridge, manager, and telnet session APIs live in split
packages and should be imported from their explicit submodules.
"""

from __future__ import annotations

import pkgutil
from importlib import import_module

# Allow other installed packages to contribute sub-packages under provide.uterm
# (e.g. provide-uterm-cloudflare contributes provide.uterm.cloudflare).
__path__ = pkgutil.extend_path(__path__, __name__)

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("provide-uterm")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

from provide.uterm.terminal_frames import TerminalFrameDisconnectedError

from provide.uterm.ansi import (
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
from provide.uterm.auth import (
    AuthorizedKeysFileResolver,
    NullResolver,
    ResolvedIdentity,
    SSHKeyResolver,
    fingerprint_from_openssh_blob,
)
from provide.uterm.colors import (
    apply_color_mode,
    downgrade_to_16,
    downgrade_to_256,
    rgb_to_16_index,
    rgb_to_256,
)
from provide.uterm.control_channel_builders import (
    make_identity,
    make_link_patterns,
    make_presence_update,
    make_resume,
    make_resume_failed,
    make_resume_ok,
    make_session_token,
)
from provide.uterm.control_channel_patterns import LinkPattern, LinkPatternRegistry
from provide.uterm.file_io import load_ans, load_palette, load_txt
from provide.uterm.line_editor import LineEditor
from provide.uterm.render.segments import (
    SEGMENT_COLOR_NAMES,
    Segment,
    ansi_to_segments,
    tokens_to_segments,
)
from provide.uterm.screen import (
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
from provide.uterm.ws_bytes import (
    channel_str_to_bytes,
    ws_frame_to_channel_str,
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
    "SEGMENT_COLOR_NAMES",
    "Segment",
    "ansi_to_segments",
    "tokens_to_segments",
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
    # ws_bytes — lossless byte ↔ str shim for the inline control channel
    "channel_str_to_bytes",
    "ws_frame_to_channel_str",
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
    # terminal frame lifecycle
    "TerminalFrameDisconnectedError",
    "fingerprint_from_openssh_blob",
    # control-plane namespaces
    "control_channel_namespace",
    "control_plane_namespace",
    "frames",
    "channels",
]

from provide.uterm.control import channel as control_channel_namespace
from provide.uterm.control import plane as control_plane_namespace


def __getattr__(name: str) -> object:
    if name == "frames":
        return import_module("provide.uterm.frames")
    if name == "channels":
        return import_module("provide.uterm.channels")
    raise AttributeError(name)
