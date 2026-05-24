#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Demo recording package — re-exports all public symbols from submodules."""

from scripts.demos.api import fanout_send, fanout_send_results, send_to_session, wait_connected
from scripts.demos.browser import (
    browser_record,
    browser_record_multi,
    click_hijack,
    open_background_context,
    record_perspective,
    record_perspective_with_background,
    type_in_terminal,
    wait_for_presence_bar,
    wait_for_status,
    wait_for_terminal,
)
from scripts.demos.ffmpeg import (
    add_title_card,
    asciinema_record,
    concat_clips,
    ffmpeg_to_mp4,
    hstack_clips,
    trim_clip,
)
from scripts.demos.fleet import record_fleet_complete, record_simultaneous_perspectives
from scripts.demos.output import (
    BASE_OUT,
    banner,
    clean_terminal_output,
    info,
    kv,
    ok,
    out_dir,
    warn,
)
from scripts.demos.server import BrowserStep, dev_bearer_headers, free_port, start_server, stop_server

__all__ = [
    "BASE_OUT",
    "BrowserStep",
    "add_title_card",
    "asciinema_record",
    "banner",
    "browser_record",
    "browser_record_multi",
    "clean_terminal_output",
    "click_hijack",
    "concat_clips",
    "dev_bearer_headers",
    "fanout_send",
    "fanout_send_results",
    "ffmpeg_to_mp4",
    "free_port",
    "hstack_clips",
    "info",
    "kv",
    "ok",
    "open_background_context",
    "out_dir",
    "record_fleet_complete",
    "record_perspective",
    "record_perspective_with_background",
    "record_simultaneous_perspectives",
    "send_to_session",
    "start_server",
    "stop_server",
    "trim_clip",
    "type_in_terminal",
    "wait_connected",
    "warn",
    "wait_for_presence_bar",
    "wait_for_status",
    "wait_for_terminal",
]
