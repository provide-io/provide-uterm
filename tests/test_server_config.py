#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Config parsing tests for the hosted terminal server."""

from __future__ import annotations

from pathlib import Path

import pytest

from provide.terminal.server.app import create_server_app
from provide.terminal.server.auth import Principal
from provide.terminal.server.config import config_from_mapping, default_server_config, load_server_config
from provide.terminal.server.models import AuthConfig, SessionDefinition
from provide.terminal.server.policy import SessionPolicyResolver


def test_default_server_config_has_demo_session() -> None:
    config = default_server_config()

    assert config.server.public_base_url == "http://127.0.0.1:8780"
    assert config.auth.mode == "dev"
    assert len(config.sessions) == 1
    assert config.sessions[0].session_id == "demo-session"
    assert config.sessions[0].connector_type == "demo"


def test_config_from_mapping_parses_sessions_and_paths() -> None:
    config = config_from_mapping(
        {
            "server": {"host": "0.0.0.0", "port": 9001, "public_base_url": "http://127.0.0.1:9001"},
            "ui": {"app_path": "ops", "assets_path": "assets"},
            "recording": {"enabled_by_default": True, "directory": "logs"},
            "sessions": [
                {
                    "session_id": "bbs",
                    "display_name": "Public BBS",
                    "connector_type": "telnet",
                    "input_mode": "hijack",
                    "host": "bbs.example.com",
                    "port": 23,
                    "tags": ["public", "bbs"],
                }
            ],
        }
    )

    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9001
    assert config.ui.app_path == "/ops"
    assert config.ui.assets_path == "/assets"
    assert config.recording.enabled_by_default is True
    assert config.recording.directory == Path("logs")
    assert len(config.sessions) == 1
    assert config.sessions[0].connector_config["host"] == "bbs.example.com"
    assert config.sessions[0].connector_config["port"] == 23


def test_policy_fails_closed_for_anonymous_in_non_dev_mode() -> None:
    policy = SessionPolicyResolver(AuthConfig(mode="jwt", jwt_public_key_pem="x", jwt_algorithms=["HS256"]))
    session = SessionDefinition(session_id="s1", display_name="Session", connector_type="demo")

    role = policy.role_for(Principal(subject_id="anonymous", roles=frozenset({"viewer"})), session)

    assert role == "viewer"


def test_load_server_config_resolves_relative_recording_path(tmp_path: Path) -> None:
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[recording]",
                'directory = "logs"',
                "",
                "[[sessions]]",
                'session_id = "demo-session"',
                'display_name = "Demo"',
                'connector_type = "demo"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_server_config(cfg_path)

    assert config.recording.directory == (tmp_path / "logs").resolve()


def test_jwt_mode_requires_worker_token() -> None:
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt", jwt_public_key_pem="x" * 64, jwt_algorithms=["HS256"], worker_bearer_token=None
    )

    with pytest.raises(ValueError, match="worker_bearer_token"):
        create_server_app(config)
