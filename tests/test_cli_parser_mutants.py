#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted mutation-killing tests for cli.py.

Tests for _build_parser: prog, subcommand destination, color-mode, and token-file.
"""

from __future__ import annotations

import pytest

from provide.terminal.cli import _build_parser

pytestmark = pytest.mark.timeout(10)


class TestBuilderParserProg:
    """Kill mutmut_2/4/6/7: prog=None/removed/wrong changes prog name."""

    def test_parser_prog_is_uterm(self) -> None:
        """mutmut_2/4/6/7: parser prog must be 'uterm'."""
        parser = _build_parser()
        assert parser.prog == "uterm"

    def test_parser_prog_not_none(self) -> None:
        """mutmut_2: prog must not be None."""
        parser = _build_parser()
        assert parser.prog is not None

    def test_parser_command_dest(self) -> None:
        """mutmut: subparsers dest must be 'command'."""
        args = _build_parser().parse_args(["proxy", "host", "23"])
        assert hasattr(args, "command")
        assert args.command == "proxy"

    def test_parser_listen_command_dest(self) -> None:
        """subparsers dest must store 'listen' for listen command."""
        args = _build_parser().parse_args(["listen", "ws://url"])
        assert args.command == "listen"


# ---------------------------------------------------------------------------
# _build_parser — listen subcommand default values that carry into behavior
# ---------------------------------------------------------------------------


class TestListenParserColorMode:
    """Kill mutants on listen --color-mode default."""

    def test_color_mode_default_is_passthrough(self) -> None:
        """Default color mode is 'passthrough'."""
        args = _build_parser().parse_args(["listen", "ws://url"])
        assert args.color_mode == "passthrough"

    def test_color_mode_choices(self) -> None:
        """color-mode accepts passthrough, 256, 16."""
        for choice in ["passthrough", "256", "16"]:
            args = _build_parser().parse_args(["listen", "ws://url", "--color-mode", choice])
            assert args.color_mode == choice

    def test_color_mode_invalid_rejected(self) -> None:
        """Invalid color-mode rejected."""
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["listen", "ws://url", "--color-mode", "invalid"])


class TestListenParserTokenFile:
    """Kill mutmut_41: Path(args.token_file) vs Path(None)."""

    def test_token_file_has_default(self) -> None:
        """mutmut_41: token_file default must be a valid path string, not None."""
        args = _build_parser().parse_args(["listen", "ws://url"])
        assert args.token_file is not None
        assert isinstance(args.token_file, str)
        assert len(args.token_file) > 0

    def test_token_file_custom(self) -> None:
        """token_file can be set to a custom path."""
        args = _build_parser().parse_args(["listen", "ws://url", "--token-file", "/tmp/mytoken"])  # noqa: S108
        assert args.token_file == "/tmp/mytoken"  # noqa: S108
