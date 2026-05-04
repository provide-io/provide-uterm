#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the server CLI entry point (uterm-server)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_cli_runs_with_defaults() -> None:
    from provide.terminal.server.cli import main

    with patch("uvicorn.run") as mock_run:
        main([])
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "127.0.0.1"
        assert isinstance(kwargs["port"], int)


def test_cli_host_override() -> None:
    from provide.terminal.server.cli import main

    with (
        patch("provide.terminal.server.cli.create_server_app", return_value=MagicMock()),
        patch("uvicorn.run") as mock_run,
    ):
        main(["--host", "0.0.0.0"])
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "0.0.0.0"


def test_cli_port_override() -> None:
    from provide.terminal.server.cli import main

    with patch("uvicorn.run") as mock_run:
        main(["--port", "9999"])
        _, kwargs = mock_run.call_args
        assert kwargs["port"] == 9999


def test_cli_host_and_port_updates_public_base_url() -> None:
    from provide.terminal.server.cli import main

    captured: dict = {}

    def _capture(app: object, **kwargs: object) -> None:
        captured.update(kwargs)

    with (
        patch("provide.terminal.server.cli.create_server_app", return_value=MagicMock()),
        patch("uvicorn.run", side_effect=_capture),
    ):
        main(["--host", "10.0.0.1", "--port", "7777"])

    assert captured["host"] == "10.0.0.1"
    assert captured["port"] == 7777


def test_cli_config_file(tmp_path: object) -> None:
    from provide.terminal.server.cli import main

    assert isinstance(tmp_path, __import__("pathlib").Path)
    cfg = tmp_path / "server.toml"
    cfg.write_text("[server]\nhost = '127.0.0.1'\nport = 8800\n")

    with patch("uvicorn.run") as mock_run:
        main(["--config", str(cfg)])
        _, kwargs = mock_run.call_args
        assert kwargs["port"] == 8800


def test_cli_https_public_base_url_preserved_on_host_override() -> None:
    from provide.terminal.server import default_server_config
    from provide.terminal.server.cli import main

    cfg = default_server_config()
    cfg.server.public_base_url = "https://myserver.example.com:443"

    with (
        patch("provide.terminal.server.cli.load_server_config", return_value=cfg),
        patch("provide.terminal.server.cli.create_server_app", return_value=MagicMock()),
        patch("uvicorn.run") as mock_run,
    ):
        main(["--host", "0.0.0.0"])
        _, kwargs = mock_run.call_args
        assert kwargs["host"] == "0.0.0.0"

    # public_base_url scheme should remain https
    assert cfg.server.public_base_url.startswith("https://")


def test_cli_app_passed_to_uvicorn() -> None:
    from fastapi import FastAPI

    from provide.terminal.server.cli import main

    with (
        patch("provide.terminal.server.cli.create_server_app", return_value=MagicMock(spec=FastAPI)) as mock_create,
        patch("uvicorn.run") as mock_run,
    ):
        main([])
        mock_create.assert_called_once()
        app_arg = mock_run.call_args[0][0]
        assert app_arg is mock_create.return_value
