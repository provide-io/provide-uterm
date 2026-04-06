#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for mcp/cli.py (uterm-mcp).

Kills surviving mutations in _build_parser — prog, description, argument names,
required, dest, action, default values, and header parsing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _build_parser — prog, description
# ---------------------------------------------------------------------------


class TestMcpCliParser:
    def test_prog_is_uterm_mcp(self) -> None:
        """parser.prog is 'uterm-mcp' (kills mutmut_3, _5, etc.)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        assert parser.prog == "uterm-mcp"

    def test_description_is_not_none(self) -> None:
        """parser.description is not None (kills mutmut_3)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        assert parser.description is not None

    def test_description_starts_with_mcp(self) -> None:
        """parser.description starts with 'MCP' (kills case mutation)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        assert parser.description.startswith("MCP")

    def test_description_contains_provide_terminal(self) -> None:
        """description mentions 'provide-terminal'."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        assert "provide-terminal" in parser.description

    def test_url_argument_exists(self) -> None:
        """--url argument is registered (kills XX--urlXX mutation)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        # Should parse --url without error
        args = parser.parse_args(["--url", "http://test"])
        assert args.url == "http://test"

    def test_url_required_true(self) -> None:
        """--url is required=True (kills required=False/None mutations)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                parser.parse_args([])
                raise AssertionError("Should have raised SystemExit")
            except SystemExit as e:
                assert e.code != 0

    def test_entity_prefix_default_is_worker(self) -> None:
        """--entity-prefix defaults to '/worker' (kills mutations to default)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--url", "http://test"])
        assert args.entity_prefix == "/worker"

    def test_entity_prefix_custom_value(self) -> None:
        """--entity-prefix accepts custom values."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--url", "http://test", "--entity-prefix", "/bot"])
        assert args.entity_prefix == "/bot"

    def test_headers_dest_is_headers(self) -> None:
        """--header stores to dest='headers' (kills dest= mutations)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--url", "http://test", "--header", "X-Foo:bar"])
        assert hasattr(args, "headers")

    def test_headers_action_is_append(self) -> None:
        """--header uses action='append' so multiple headers accumulate."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(
            [
                "--url",
                "http://test",
                "--header",
                "X-A:1",
                "--header",
                "X-B:2",
            ]
        )
        assert len(args.headers) == 2
        assert "X-A:1" in args.headers
        assert "X-B:2" in args.headers

    def test_headers_default_is_empty_list(self) -> None:
        """--header defaults to [] (kills default=None mutation)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["--url", "http://test"])
        assert args.headers == []

    def test_header_help_text_not_none(self) -> None:
        """--header has a help string (kills help=None mutation)."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        # Find the --header action
        action = next(a for a in parser._actions if "--header" in (a.option_strings or []))
        assert action.help is not None
        assert len(action.help) > 0

    def test_url_help_text_not_none(self) -> None:
        """--url has a help string."""
        from provide.terminal.ai.cli import _build_parser

        parser = _build_parser()
        action = next(a for a in parser._actions if "--url" in (a.option_strings or []))
        assert action.help is not None


# ---------------------------------------------------------------------------
# main() — header parsing, app creation
# ---------------------------------------------------------------------------


class TestMcpCliMain:
    def test_main_requires_url(self) -> None:
        """main() exits with error if --url not provided."""
        import contextlib
        import io

        from provide.terminal.ai.cli import main

        with contextlib.redirect_stderr(io.StringIO()):
            try:
                main([])
                raise AssertionError("Should have raised SystemExit")
            except SystemExit as e:
                assert e.code != 0

    def test_main_parses_url(self) -> None:
        """main() passes --url to create_mcp_app."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        with patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app) as mock_create:
            main(["--url", "http://localhost:8780"])

        mock_create.assert_called_once()
        assert mock_create.call_args[0][0] == "http://localhost:8780"

    def test_main_parses_entity_prefix(self) -> None:
        """main() passes entity_prefix to create_mcp_app."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        with patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app) as mock_create:
            main(["--url", "http://test", "--entity-prefix", "/mybot"])

        kwargs = mock_create.call_args[1]
        assert kwargs["entity_prefix"] == "/mybot"

    def test_main_parses_single_header(self) -> None:
        """main() parses 'Key:Value' header format correctly."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        with patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app) as mock_create:
            main(["--url", "http://test", "--header", "Authorization:Bearer tok"])

        kwargs = mock_create.call_args[1]
        headers = kwargs.get("headers")
        assert headers is not None
        assert headers.get("Authorization") == "Bearer tok"

    def test_main_parses_multiple_headers(self) -> None:
        """main() accumulates multiple --header values."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        with patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app) as mock_create:
            main(["--url", "http://test", "--header", "X-A:1", "--header", "X-B:2"])

        kwargs = mock_create.call_args[1]
        headers = kwargs["headers"]
        assert headers["X-A"] == "1"
        assert headers["X-B"] == "2"

    def test_main_no_headers_passes_none(self) -> None:
        """main() passes headers=None when no --header args given."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        with patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app) as mock_create:
            main(["--url", "http://test"])

        kwargs = mock_create.call_args[1]
        assert kwargs["headers"] is None

    def test_main_calls_app_run_with_stdio(self) -> None:
        """main() calls app.run(transport='stdio')."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        with patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app):
            main(["--url", "http://test"])

        mock_app.run.assert_called_once_with(transport="stdio")

    def test_main_uses_sys_argv_when_argv_is_none(self) -> None:
        """main() with argv=None uses sys.argv[1:]."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        # sys.argv[1:] is typically ['--url', 'http://x'] in the test
        with (
            patch("sys.argv", ["uterm-mcp", "--url", "http://sys-argv-test"]),
            patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app) as mock_create,
        ):
            main(None)

        assert mock_create.call_args[0][0] == "http://sys-argv-test"

    def test_header_partition_splits_on_colon(self) -> None:
        """Header 'Key: Value' is parsed correctly (key strip, value strip)."""
        from provide.terminal.ai.cli import main

        mock_app = MagicMock()
        mock_app.run = MagicMock()

        with patch("provide.terminal.ai.server.create_mcp_app", return_value=mock_app) as mock_create:
            main(["--url", "http://test", "--header", "  X-Token : secret  "])

        kwargs = mock_create.call_args[1]
        headers = kwargs["headers"]
        assert "X-Token" in headers
        assert headers["X-Token"] == "secret"
