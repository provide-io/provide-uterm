#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Tests for the uterm-cf CLI (cli.py) and ui/assets.py coverage gaps."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from provide.terminal.cloudflare.cli import build_parser, cmd_build, cmd_dev, main

# ---------------------------------------------------------------------------
# cli.py — build subcommand
# ---------------------------------------------------------------------------


class TestCmdBuild:
    def test_build_ok_when_required_files_exist(self) -> None:
        args = SimpleNamespace()
        result = cmd_build(args)
        assert result == 0

    def test_build_fails_when_entry_missing(self, tmp_path: Path) -> None:
        """Missing wrangler.toml → error."""
        with patch("provide.terminal.cloudflare.cli._package_root", return_value=tmp_path):
            args = SimpleNamespace()
            result = cmd_build(args)
        assert result == 1

    def test_build_fails_when_entry_py_missing(self, tmp_path: Path) -> None:
        """wrangler.toml present but entry.py missing → error."""
        (tmp_path / "wrangler.toml").write_text("")
        with patch("provide.terminal.cloudflare.cli._package_root", return_value=tmp_path):
            args = SimpleNamespace()
            result = cmd_build(args)
        assert result == 1


# ---------------------------------------------------------------------------
# cli.py — dev / deploy subcommands
# ---------------------------------------------------------------------------


class TestCmdDevDeploy:
    def test_cmd_dev_calls_pywrangler(self) -> None:
        args = SimpleNamespace(ip="127.0.0.1", port=8787)
        with (
            patch("provide.terminal.cloudflare.cli._require_pywrangler"),
            patch("provide.terminal.cloudflare.cli._run", return_value=0) as mock_run,
        ):
            result = cmd_dev(args)
        assert result == 0
        cmd_called = mock_run.call_args[0][0]
        assert "pywrangler" in cmd_called
        assert "dev" in cmd_called

    def test_cmd_deploy_calls_pywrangler(self) -> None:
        from provide.terminal.cloudflare.cli import cmd_deploy

        args = SimpleNamespace(env="production", extra="")
        with (
            patch("provide.terminal.cloudflare.cli._require_pywrangler"),
            patch("provide.terminal.cloudflare.cli._run", return_value=0) as mock_run,
        ):
            result = cmd_deploy(args)
        assert result == 0
        cmd_called = mock_run.call_args[0][0]
        assert "pywrangler" in cmd_called
        assert "deploy" in cmd_called

    def test_cmd_deploy_passes_extra_args(self) -> None:
        from provide.terminal.cloudflare.cli import cmd_deploy

        args = SimpleNamespace(env="staging", extra="--dry-run")
        with (
            patch("provide.terminal.cloudflare.cli._require_pywrangler"),
            patch("provide.terminal.cloudflare.cli._run", return_value=0) as mock_run,
        ):
            cmd_deploy(args)
        cmd_called = mock_run.call_args[0][0]
        assert "--dry-run" in cmd_called

    def test_require_pywrangler_raises_when_not_found(self) -> None:
        from provide.terminal.cloudflare.cli import _require_pywrangler

        with patch("shutil.which", return_value=None), pytest.raises(RuntimeError, match="pywrangler not found"):
            _require_pywrangler()


# ---------------------------------------------------------------------------
# cli.py — main entry point
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_build_subcommand(self) -> None:
        with patch("provide.terminal.cloudflare.cli.cmd_build", return_value=0) as mock:
            result = main(["build"])
        assert result == 0
        mock.assert_called_once()

    def test_main_runtime_error_returns_1(self) -> None:
        with patch("provide.terminal.cloudflare.cli.cmd_build", side_effect=RuntimeError("oops")):
            result = main(["build"])
        assert result == 1

    def test_main_no_subcommand_exits(self) -> None:
        with pytest.raises(SystemExit):
            main([])

    def test_run_helper_invokes_subprocess(self) -> None:
        from provide.terminal.cloudflare.cli import _run

        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as mock_sp:
            result = _run(["echo", "hi"], Path())
        assert result == 0
        mock_sp.assert_called_once()

    def test_main_module_import(self) -> None:
        import importlib

        mod = importlib.import_module("provide.terminal.cloudflare.__main__")
        assert hasattr(mod, "main")

    def test_build_parser_has_expected_subcommands(self) -> None:
        parser = build_parser()
        # Should not raise
        args = parser.parse_args(["build"])
        assert args.command == "build"
        args = parser.parse_args(["dev", "--port", "9000"])
        assert args.port == 9000
        args = parser.parse_args(["deploy", "--env", "prod"])
        assert args.env == "prod"


# ---------------------------------------------------------------------------
# ui/assets.py — Path(__file__) fallback and OSError branch
# ---------------------------------------------------------------------------


class TestServeAssetFallbacks:
    def test_path_file_fallback_serves_existing_file(self, tmp_path: Path) -> None:
        """Path(__file__).parent/static fallback serves file when importlib.resources reports no match."""
        from provide.terminal.cloudflare.ui import assets as assets_mod

        # Create a fake static file
        fake_static = tmp_path / "static"
        fake_static.mkdir()
        (fake_static / "test.js").write_text("console.log('hi')")

        # Patch _LOCAL_STATIC to point at our tmp dir, and make importlib.resources miss
        with (
            patch.object(assets_mod, "_LOCAL_STATIC", fake_static),
            patch("importlib.resources.files", side_effect=ModuleNotFoundError),
        ):
            resp = assets_mod.serve_asset("test.js")
        assert resp.status == 200
        assert "console.log" in resp.body

    def test_path_file_fallback_oserror_falls_through(self, tmp_path: Path) -> None:
        """OSError on _LOCAL_STATIC read falls through to provide.terminal package."""
        from provide.terminal.cloudflare.ui import assets as assets_mod

        fake_static = tmp_path / "static"
        fake_static.mkdir()
        target = fake_static / "test.js"
        target.write_text("content")

        def bad_is_file(self: Path) -> bool:
            raise OSError("disk error")

        with (
            patch.object(assets_mod, "_LOCAL_STATIC", fake_static),
            patch("importlib.resources.files", side_effect=ModuleNotFoundError),
            patch.object(Path, "is_file", bad_is_file),
        ):
            resp = assets_mod.serve_asset("test.js")
        # Falls through to provide.terminal fallback → "asset package unavailable" if not installed
        # OR serves from provide.terminal if installed. Either way, not a crash.
        assert resp.status in {200, 404}
