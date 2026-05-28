#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Config parsing tests for the hosted terminal server."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server.app import create_server_app
from provide.uterm.server.auth import Principal
from provide.uterm.server.config import config_from_mapping, default_server_config, load_server_config
from provide.uterm.server.models import AuthConfig, RecordingConfig, SessionDefinition, validation_error_message
from provide.uterm.server.policy import SessionPolicyResolver


def _load_config_schema_by_source_path() -> ModuleType:
    module_name = "packages.provide-uterm-server.src.provide.uterm.server.config_schema"
    source_path = Path(__file__).resolve().parents[2] / "src/provide/uterm/server/config_schema.py"
    spec = importlib.util.spec_from_file_location(module_name, source_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_default_server_config_has_demo_session() -> None:
    config = default_server_config()

    assert config.server.public_base_url == "http://127.0.0.1:8780"
    assert config.auth.mode == "dev_token"
    assert config.control_plane.backend == "memory"
    assert config.control_plane.database_url is None
    assert len(config.sessions) == 1
    assert config.sessions[0].session_id == "provide-shell"
    assert config.sessions[0].connector_type == "shell"


def test_builtin_connector_type_validates_when_registry_is_partially_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    from provide.uterm.server import connectors

    monkeypatch.setattr(connectors, "registered_types", lambda: frozenset({"pty"}))

    session = SessionDefinition(session_id="provide-shell", connector_type="shell")

    assert session.connector_type == "shell"


def test_clean_path_normalizes_edge_cases() -> None:
    _clean_path = _load_config_schema_by_source_path()._clean_path

    assert _clean_path("admin", "/fallback") == "/admin"
    assert _clean_path("/admin/", "/fallback") == "/admin"
    assert _clean_path("/adminX/", "/fallback") == "/adminX"
    assert _clean_path("", "/fallback") == "/fallback"
    assert _clean_path("///", "/fallback") == "/"


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
    assert config.control_plane.backend == "memory"
    assert config.control_plane.database_url is None
    assert len(config.sessions) == 1
    assert config.sessions[0].connector_config["host"] == "bbs.example.com"
    assert config.sessions[0].connector_config["port"] == 23


async def test_policy_fails_closed_for_anonymous_in_non_dev_mode() -> None:
    policy = SessionPolicyResolver(AuthConfig(mode="jwt", jwt_public_key_pem="x", jwt_algorithms=["HS256"]))
    session = SessionDefinition(session_id="s1", display_name="Session", connector_type="shell")

    role = await policy.role_for(Principal(subject_id="anonymous", roles=frozenset({"viewer"})), session)

    assert role == "viewer"


def test_auth_config_requires_proxy_secret() -> None:
    # Allowed: not required, not provided
    config = AuthConfig(require_upstream_proxy_secret=False)
    assert config.require_upstream_proxy_secret is False

    # Allowed: required, provided
    config2 = AuthConfig(require_upstream_proxy_secret=True, upstream_proxy_secret="secret")
    assert config2.upstream_proxy_secret == "secret"

    # Error: required, not provided
    with pytest.raises(
        ValueError, match="auth.upstream_proxy_secret is required when auth.require_upstream_proxy_secret=True"
    ):
        AuthConfig(require_upstream_proxy_secret=True)

    with pytest.raises(
        ValueError, match="auth.upstream_proxy_secret is required when auth.require_upstream_proxy_secret=True"
    ):
        AuthConfig(require_upstream_proxy_secret=True, upstream_proxy_secret="  ")


def test_load_server_config_resolves_relative_recording_path(tmp_path: Path) -> None:
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[recording]",
                'directory = "logs"',
                "",
                "[[sessions]]",
                'session_id = "provide-shell"',
                'display_name = "Demo"',
                'connector_type = "shell"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_server_config(cfg_path)

    assert config.recording.directory == (tmp_path / "logs").resolve()


def test_checked_in_docker_config_fails_closed_until_configured() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    config = load_server_config(repo_root / "docker" / "server.toml")

    assert config.server.host == "0.0.0.0"
    with pytest.raises(ValueError, match="placeholder"):
        create_server_app(config, api_only=True)


def test_load_server_config_parses_ephemeral_and_max_sessions(tmp_path: Path) -> None:
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[server]",
                "max_sessions = 3",
                "",
                "[[sessions]]",
                'session_id = "ephemeral-shell"',
                'display_name = "Ephemeral Shell"',
                'connector_type = "shell"',
                "ephemeral = true",
            ]
        ),
        encoding="utf-8",
    )

    config = load_server_config(cfg_path)

    assert config.server.max_sessions == 3
    assert len(config.sessions) == 1
    assert config.sessions[0].ephemeral is True
    assert "ephemeral" not in config.sessions[0].connector_config


def test_load_server_config_parses_control_plane_section(tmp_path: Path) -> None:
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text(
        "\n".join(
            [
                "[control_plane]",
                'backend = "sqlite"',
                'database_url = "sqlite+aiosqlite:///tmp/control-plane.db"',
                "",
                "[[sessions]]",
                'session_id = "provide-shell"',
                'display_name = "Demo"',
                'connector_type = "shell"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_server_config(cfg_path)

    assert config.control_plane.backend == "sqlite"
    assert config.control_plane.database_url == "sqlite+aiosqlite:///tmp/control-plane.db"


def test_partial_control_plane_override_preserves_sibling_defaults() -> None:
    config = config_from_mapping({"control_plane": {"database_url": "sqlite+aiosqlite:///tmp/cp.db"}})

    assert config.control_plane.backend == "memory"
    assert config.control_plane.database_url == "sqlite+aiosqlite:///tmp/cp.db"


def test_config_from_mapping_rejects_sqlite_control_plane_without_database_url() -> None:
    with pytest.raises(ValueError, match="control_plane\\.database_url"):
        config_from_mapping({"control_plane": {"backend": "sqlite"}})


def test_jwt_mode_requires_worker_token() -> None:
    config = default_server_config()
    config.auth = AuthConfig(
        mode="jwt", jwt_public_key_pem="x" * 64, jwt_algorithms=["HS256"], worker_bearer_token=None
    )

    with pytest.raises(ValueError, match="worker_bearer_token"):
        create_server_app(config)


def test_config_fitaddon_cdn_roundtrips() -> None:
    config = config_from_mapping({"ui": {"fitaddon_cdn": "https://example.com/fitaddon"}})
    assert config.ui.fitaddon_cdn == "https://example.com/fitaddon"


def test_session_definition_collects_unknown_fields_into_connector_config() -> None:
    session = SessionDefinition.model_validate(
        {"session_id": "bbs", "connector_type": "telnet", "host": "bbs.example.com"}
    )

    assert session.display_name == "bbs"
    assert session.connector_config == {"host": "bbs.example.com"}


def test_session_definition_validator_helpers_cover_non_mapping_paths() -> None:
    assert SessionDefinition._collect_connector_config("not-a-dict") == "not-a-dict"

    class _Info:
        data = None

    assert SessionDefinition._validate_display_name("", _Info()) == ""
    assert SessionDefinition._validate_connector_type("shell", _Info()) == "shell"

    with pytest.raises(ValueError, match="invalid input_mode for <unknown>: bad"):
        SessionDefinition._validate_input_mode("bad", _Info())
    with pytest.raises(ValueError, match="invalid visibility for <unknown>: 'secret'"):
        SessionDefinition._validate_visibility("secret", _Info())


def test_session_definition_validator_helpers_cover_remaining_schema_paths() -> None:
    class _Info:
        data = {"session_id": "s1"}

    assert SessionDefinition._validate_display_name("", _Info()) == "s1"
    with pytest.raises(ValueError, match=r"invalid connector_type for 's1': 'bogus'"):
        SessionDefinition._validate_connector_type("bogus", _Info())
    with pytest.raises(ValueError, match="recording.max_bytes must be >= 0"):
        RecordingConfig(max_bytes=-1)
    with pytest.raises(ValueError, match="recording.retention_s must be >= 0"):
        RecordingConfig(retention_s=-1)


def test_config_from_mapping_skips_non_dict_session_entry() -> None:
    # Line 119: non-dict entries in sessions_data are skipped with `continue`
    config = config_from_mapping({"sessions": ["not-a-dict", {"session_id": "s1", "connector_type": "shell"}]})
    assert len(config.sessions) == 1
    assert config.sessions[0].session_id == "s1"


def test_config_from_mapping_empty_sessions_list_clears_defaults() -> None:
    """sessions: [] must disable the default shell session, not be treated as absent."""
    config = config_from_mapping({"sessions": []})
    assert config.sessions == []


def test_config_from_mapping_rejects_empty_session_id() -> None:
    with pytest.raises(ValueError, match="session_id is required"):
        config_from_mapping({"sessions": [{"session_id": "", "connector_type": "shell"}]})


def test_config_from_mapping_rejects_invalid_session_id_chars() -> None:
    with pytest.raises(ValueError, match=r"session_id must match"):
        config_from_mapping({"sessions": [{"session_id": "bad id!", "connector_type": "shell"}]})


def test_config_from_mapping_rejects_invalid_input_mode() -> None:
    with pytest.raises(ValueError, match="invalid input_mode"):
        config_from_mapping({"sessions": [{"session_id": "s1", "connector_type": "shell", "input_mode": "bad"}]})


def test_config_from_mapping_rejects_invalid_visibility() -> None:
    with pytest.raises(ValueError, match="invalid visibility"):
        config_from_mapping({"sessions": [{"session_id": "s1", "connector_type": "shell", "visibility": "test"}]})


def test_partial_auth_override_preserves_default_server_config_auth_mode() -> None:
    # Regression: replacing merged["auth"] with the raw user dict discarded
    # default_server_config().auth.mode == "dev_token", falling back to
    # AuthConfig's class default "jwt".
    config = config_from_mapping({"auth": {"principal_header": "x-user"}})

    assert config.auth.mode == "dev_token"
    assert config.auth.principal_header == "x-user"


def test_partial_server_override_preserves_sibling_defaults() -> None:
    config = config_from_mapping({"server": {"port": 9999}})

    assert config.server.port == 9999
    assert config.server.host == default_server_config().server.host
    assert config.server.public_base_url == default_server_config().server.public_base_url


def test_partial_ui_override_preserves_sibling_defaults() -> None:
    config = config_from_mapping({"ui": {"app_path": "/custom"}})

    assert config.ui.app_path == "/custom"
    assert config.ui.xterm_cdn == default_server_config().ui.xterm_cdn
    assert config.ui.fitaddon_cdn == default_server_config().ui.fitaddon_cdn


def test_partial_recording_override_preserves_sibling_defaults() -> None:
    config = config_from_mapping({"recording": {"enabled_by_default": True}})

    assert config.recording.enabled_by_default is True
    assert config.recording.max_bytes == default_server_config().recording.max_bytes
    assert config.recording.retention_s == default_server_config().recording.retention_s
    assert config.recording.directory == default_server_config().recording.directory


def test_partial_recording_override_accepts_control_channel_mode() -> None:
    config = config_from_mapping({"recording": {"control_channel_mode": "wire"}})

    assert config.recording.control_channel_mode == "wire"
    assert config.recording.max_bytes == default_server_config().recording.max_bytes
    assert config.recording.directory == default_server_config().recording.directory


def test_partial_recording_override_accepts_retention_s() -> None:
    config = config_from_mapping({"recording": {"retention_s": 3600}})
    assert config.recording.retention_s == 3600


def test_config_from_mapping_rejects_negative_recording_retention_s() -> None:
    with pytest.raises(ValueError, match="recording.retention_s must be >= 0"):
        config_from_mapping({"recording": {"retention_s": -1}})


def test_config_from_mapping_rejects_invalid_control_channel_mode() -> None:
    with pytest.raises(ValueError, match="exclude.*wire"):
        config_from_mapping({"recording": {"control_channel_mode": "bogus"}})


@pytest.mark.parametrize(
    "section",
    ["server", "auth", "ui", "recording", "profiles", "security", "tunnel", "pam", "control_plane"],
)
def test_config_from_mapping_rejects_non_dict_known_section(section: str) -> None:
    # Malformed TOML (e.g. `server = []`) must raise ValueError, not TypeError.
    with pytest.raises(ValueError, match=rf"\[{section}\] must be a table"):
        config_from_mapping({section: []})


def test_config_from_mapping_rejects_unknown_top_level_section() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        config_from_mapping({"bogus": {"x": 1}})


def test_config_from_mapping_accepts_session_idle_timeout_s() -> None:
    config = config_from_mapping({"session_idle_timeout_s": 120})
    assert config.session_idle_timeout_s == 120


def test_config_from_mapping_accepts_session_retention_s() -> None:
    config = config_from_mapping({"session_retention_s": 3600})
    assert config.session_retention_s == 3600


def test_config_from_mapping_accepts_browser_rate_limit_per_sec() -> None:
    config = config_from_mapping({"browser_rate_limit_per_sec": 50.0})
    assert config.browser_rate_limit_per_sec == 50.0


def test_config_from_mapping_accepts_tunnel_section() -> None:
    config = config_from_mapping({"tunnel": {"token_ttl_s": 7200}})
    assert config.tunnel.token_ttl_s == 7200


def test_config_from_mapping_rejects_tunnel_ttl_under_minimum() -> None:
    with pytest.raises(ValueError, match="tunnel\\.token_ttl_s must be >= 60"):
        config_from_mapping({"tunnel": {"token_ttl_s": 59}})


def test_config_from_mapping_accepts_security_section() -> None:
    config = config_from_mapping({"security": {"mode": "dev"}})
    assert config.security.mode == "dev"


def test_config_from_mapping_accepts_profiles_security_tunnel_pam_together() -> None:
    """H2: config with profiles, security, tunnel, and pam top-level keys must be accepted."""
    config = config_from_mapping(
        {
            "profiles": {"directory": ".custom-profiles"},
            "security": {"mode": "dev"},
            "tunnel": {"token_ttl_s": 3600},
            "pam": {"auto_session": True},
        }
    )
    assert str(config.profiles.directory) == ".custom-profiles"
    assert config.security.mode == "dev"
    assert config.tunnel.token_ttl_s == 3600
    assert config.pam.auto_session is True


def test_config_from_mapping_rejects_unknown_nested_field() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        config_from_mapping({"server": {"host": "127.0.0.1", "bogus": True}})


def test_validation_error_message_handles_empty_and_prefixed_errors() -> None:
    class _EmptyError:
        def errors(self, *, include_url: bool = False) -> list[dict[str, object]]:
            return []

        def __str__(self) -> str:
            return "empty-error"

    class _PrefixedError:
        def errors(self, *, include_url: bool = False) -> list[dict[str, object]]:
            return [{"msg": "sessions exploded", "loc": ("sessions", 0)}]

        def __str__(self) -> str:
            return "prefixed-error"

    assert validation_error_message(_EmptyError()) == "empty-error"
    assert validation_error_message(_PrefixedError()) == "sessions exploded"


def test_server_bind_config_derives_url_from_custom_host_port() -> None:
    from provide.uterm.server.models import ServerBindConfig

    cfg = ServerBindConfig(host="10.0.0.1", port=9090)
    assert cfg.public_base_url == "http://10.0.0.1:9090"


def test_server_bind_config_preserves_explicit_public_base_url() -> None:
    from provide.uterm.server.models import ServerBindConfig

    cfg = ServerBindConfig(host="10.0.0.1", port=9090, public_base_url="https://proxy.example.com")
    assert cfg.public_base_url == "https://proxy.example.com"


# ---------------------------------------------------------------------------
# Mutation-killing tests for default_server_config
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Mutation-killing tests for _merged_config_mapping (model_dump mode)
# ---------------------------------------------------------------------------


def test_config_from_mapping_produces_correct_datetime_type() -> None:
    """Kills mutmut_17 (mode=None), mutmut_18 (mode=XXpythonXX), mutmut_19 (mode=PYTHON).

    _merged_config_mapping calls default_server_config().model_dump(mode='python').
    With mode='python', created_at is a datetime object (not a string).
    With mode=None, pydantic uses 'python' mode by default (may be equivalent),
    but mode='XXpythonXX' raises or gives wrong result.
    We verify round-tripping produces a valid ServerConfig with datetime intact.
    """
    from datetime import datetime

    from provide.uterm.server.models import ServerConfig

    # config_from_mapping() internally uses _merged_config_mapping which calls model_dump(mode='python')
    config = config_from_mapping({})
    assert isinstance(config, ServerConfig)
    # If mode was wrong, the sessions created_at would be a string instead of datetime
    session = config.sessions[0]
    assert isinstance(session.created_at, datetime), (
        f"session.created_at must be a datetime (model_dump mode='python'), got {type(session.created_at)}"
    )


def test_config_from_mapping_non_dict_section_error_mentions_type(section: str = "server") -> None:
    """Kills mutmut_33: error message uses type(data[section]).__name__ not type(None).__name__.

    The error must mention the actual type of the bad value, not always 'NoneType'.
    """
    with pytest.raises(ValueError) as exc_info:
        config_from_mapping({"server": ["not", "a", "dict"]})
    msg = str(exc_info.value)
    # Should mention 'list', not 'NoneType'
    assert "list" in msg, f"Error must mention actual type 'list', got: {msg!r}"
    assert "NoneType" not in msg, f"Error must not say 'NoneType', got: {msg!r}"


def test_loaded_max_sessions_is_enforced_by_app() -> None:
    config = config_from_mapping({"server": {"max_sessions": 1}})
    config.auth.mode = "header"
    config.auth.header_mode_acknowledged = True
    config.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"

    with TestClient(create_server_app(config)) as client:
        response = client.post("/api/connect", json={"connector_type": "shell"})

    assert response.status_code == 409
    assert response.json()["detail"] == "session limit reached: max_sessions=1"
