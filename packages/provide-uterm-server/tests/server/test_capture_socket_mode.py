#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""``pam.capture_socket_mode``: who may connect to the capture sockets the server binds.

Owner-only (0o600) by default. 0o660 lets a server that runs as a user of its
own share its capture sockets with the producers (the shim, a session
supervisor) through the group of a setgid directory. It holds for every
capture session, whoever created it: PAM, the sessions API with a socket of
its own, or a restart that re-creates a kept one. Nothing else is accepted.
"""

from __future__ import annotations

import json
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Importing the connector registers pty_capture.
from provide.uterm.pty.capture_connector import CaptureConnector  # noqa: F401
from provide.uterm.recording import LocalFileRecordingStore
from provide.uterm.server.app import create_server_app
from provide.uterm.server.bridge.hub import TermHub
from provide.uterm.server.capture_state import CaptureStateStore, restore_capture_sessions
from provide.uterm.server.config import config_from_mapping
from provide.uterm.server.config_schema import PamConfig, RecordingConfig, UtermServerConfig
from provide.uterm.server.models import SessionDefinition
from provide.uterm.server.registry import SessionRegistry
from provide.uterm.server.routes.health import CAPTURE_SOCKET_MODE_FEATURE, server_features
from provide.uterm.server.runtime import HostedSessionRuntime

SID = "pam-suokki-capture-4242-777"


# ── the setting ──────────────────────────────────────────────────────────────


def test_the_mode_defaults_to_owner_only() -> None:
    assert PamConfig().capture_socket_mode == 0o600


@pytest.mark.parametrize("mode", [0o600, 0o660])
def test_owner_only_and_owner_and_group_are_accepted(mode: int) -> None:
    assert config_from_mapping({"pam": {"capture_socket_mode": mode}}).pam.capture_socket_mode == mode


def test_the_mode_is_read_from_toml_written_in_octal(tmp_path: Path) -> None:
    from provide.uterm.server.config import load_server_config

    path = tmp_path / "server.toml"
    path.write_text("[pam]\ncapture_socket_mode = 0o660\n")
    assert load_server_config(path).pam.capture_socket_mode == 0o660


@pytest.mark.parametrize(
    ("mode", "shown"),
    [
        (0o666, "0o666"),
        (0o664, "0o664"),
        (0o640, "0o640"),
        (0o700, "0o700"),
        (0o770, "0o770"),
        (0o2660, "0o2660"),
        (0, "0o0"),
        (-1, "-0o1"),
    ],
)
def test_any_other_mode_is_refused_at_load_naming_the_key(mode: int, shown: str) -> None:
    with pytest.raises(ValueError) as caught:
        config_from_mapping({"pam": {"capture_socket_mode": mode}})
    assert str(caught.value) == f"Value error, pam.capture_socket_mode must be 0o600 or 0o660, got: {shown}"


@pytest.mark.parametrize("mode", ["0o660", "432", "660", 432.0, True, None, [432]])
def test_a_mode_that_is_not_an_integer_is_refused_at_load(mode: object) -> None:
    with pytest.raises(ValueError) as caught:
        config_from_mapping({"pam": {"capture_socket_mode": mode}})
    assert str(caught.value) == "Value error, pam.capture_socket_mode must be an integer: 0o600 or 0o660"


# ── what it is handed to ─────────────────────────────────────────────────────


def _definition(connector_type: str, connector_config: dict[str, Any]) -> SessionDefinition:
    return SessionDefinition(
        session_id="s1", display_name="s1", connector_type=connector_type, connector_config=connector_config
    )


def _runtime_for(definition: SessionDefinition, **kwargs: Any) -> HostedSessionRuntime:
    return HostedSessionRuntime(definition, public_base_url="http://127.0.0.1:9", recording=RecordingConfig(), **kwargs)


def test_a_runtime_hands_a_capture_connector_the_mode_over_the_stored_one() -> None:
    runtime = _runtime_for(
        _definition("pty_capture", {"socket_path": "/run/x.sock", "socket_mode": 0o600}), capture_socket_mode=0o660
    )
    effective = runtime._effective_connector_config(runtime.definition.connector_config)
    assert effective == {"socket_path": "/run/x.sock", "socket_mode": 0o660, "input_mode": "open"}


def test_a_runtime_hands_a_capture_connector_owner_only_by_default() -> None:
    runtime = _runtime_for(_definition("pty_capture", {"socket_path": "/run/x.sock"}))
    assert runtime._effective_connector_config(runtime.definition.connector_config)["socket_mode"] == 0o600


def test_a_runtime_hands_no_other_connector_a_mode() -> None:
    runtime = _runtime_for(_definition("shell", {"command": "/bin/sh"}), capture_socket_mode=0o660)
    assert "socket_mode" not in runtime._effective_connector_config(runtime.definition.connector_config)


def _registry(tmp_path: Path, *, mode: int | None = None, store: CaptureStateStore | None = None) -> SessionRegistry:
    kwargs: dict[str, Any] = {} if mode is None else {"capture_socket_mode": mode}
    return SessionRegistry(
        [],
        hub=TermHub(),
        public_base_url="http://127.0.0.1:9",
        recording=RecordingConfig(directory=tmp_path / "recordings"),
        recording_store=LocalFileRecordingStore(tmp_path / "recordings"),
        capture_state=store,
        **kwargs,
    )


@pytest.mark.parametrize(("mode", "expected"), [(None, 0o600), (0o660, 0o660)])
def test_the_registry_builds_every_runtime_with_its_mode(tmp_path: Path, mode: int | None, expected: int) -> None:
    registry = _registry(tmp_path, mode=mode)
    with patch("provide.uterm.server.registry.HostedSessionRuntime") as runtime:
        registry._runtime_for(_definition("pty_capture", {"socket_path": "/run/x.sock"}))
    assert runtime.call_args.kwargs["capture_socket_mode"] == expected


@pytest.mark.parametrize("mode", [0o600, 0o660])
def test_the_server_builds_its_registry_with_the_configured_mode(mode: int) -> None:
    config = UtermServerConfig.model_validate({"pam": {"capture_socket_mode": mode}})
    app = create_server_app(config, api_only=True)
    assert app.state.uterm_registry._capture_socket_mode == mode


# ── the sockets themselves ───────────────────────────────────────────────────


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    # Short, under TMPDIR: a Unix socket path is at most ~104 bytes, which a
    # pytest tmp_path can exceed on its own.
    with tempfile.TemporaryDirectory(prefix="cm") as td:
        yield Path(td)


def _pam_payload(socket: Path, session_id: str = SID) -> dict[str, Any]:
    # What PAM creates (pam_integration._create_capture_session), not started.
    return {
        "session_id": session_id,
        "display_name": "suokki (pts/3) [live]",
        "connector_type": "pty_capture",
        "connector_config": {"socket_path": str(socket)},
        "input_mode": "open",
        "auto_start": False,
        "ephemeral": False,
        "tags": ["pam", "capture", "suokki"],
        "visibility": "operator",
    }


def _api_payload(socket: Path) -> dict[str, Any]:
    # A session made through the sessions API with a capture socket of its own
    # (a play mirror beside the PAM sockets).
    return {
        "session_id": "play-7",
        "display_name": "play mirror",
        "connector_type": "pty_capture",
        "connector_config": {"socket_path": str(socket), "cols": 80, "rows": 24},
        "auto_start": False,
    }


async def _bound_mode(registry: SessionRegistry, session_id: str, socket: Path) -> int:
    runtime = registry.get_runtime(session_id)
    assert runtime is not None
    connector = await runtime._start_connector()
    try:
        return stat.S_IMODE(socket.stat().st_mode)
    finally:
        await connector.stop()


@pytest.mark.parametrize(("mode", "expected"), [(None, 0o600), (0o660, 0o660)])
async def test_a_pam_capture_socket_is_bound_with_the_mode(
    tmp_path: Path, socket_dir: Path, mode: int | None, expected: int
) -> None:
    registry = _registry(tmp_path, mode=mode)
    socket = socket_dir / "uterm-cap-4242.sock"
    await registry.create_session(_pam_payload(socket))
    assert await _bound_mode(registry, SID, socket) == expected


@pytest.mark.parametrize(("mode", "expected"), [(None, 0o600), (0o660, 0o660)])
async def test_an_api_capture_socket_is_bound_with_the_mode(
    tmp_path: Path, socket_dir: Path, mode: int | None, expected: int
) -> None:
    registry = _registry(tmp_path, mode=mode)
    socket = socket_dir / "uterm-play-7.sock"
    await registry.create_session(_api_payload(socket))
    assert await _bound_mode(registry, "play-7", socket) == expected


async def test_an_api_session_cannot_ask_for_a_wider_mode_than_the_server_binds(
    tmp_path: Path, socket_dir: Path
) -> None:
    registry = _registry(tmp_path)
    socket = socket_dir / "uterm-play-7.sock"
    payload = _api_payload(socket)
    payload["connector_config"]["socket_mode"] = 0o660
    await registry.create_session(payload)
    assert await _bound_mode(registry, "play-7", socket) == 0o600


async def test_a_restored_capture_socket_is_bound_with_the_mode(tmp_path: Path, socket_dir: Path) -> None:
    socket = socket_dir / "uterm-cap-4242.sock"
    before = _registry(tmp_path, store=_store(tmp_path))
    await before.create_session(_pam_payload(socket))
    await before.shutdown()
    # The mode is the server's, never the session's: the file does not carry it.
    live = json.loads((tmp_path / "state" / "live" / f"{SID}.json").read_text())
    assert live["session"]["connector_config"] == {"socket_path": str(socket)}

    store = _store(tmp_path)
    after = _registry(tmp_path, mode=0o660, store=store)
    with patch("provide.uterm.server.pam_integration._process_start_ticks", lambda pid: 777 if pid == 4242 else None):
        await restore_capture_sessions(after, store, lambda _path: True)
    assert await _bound_mode(after, SID, socket) == 0o660


def _store(tmp_path: Path) -> CaptureStateStore:
    return CaptureStateStore(tmp_path / "state", persist=True, keep_days=0, scrollback_lines=100)


# ── what /api/health says ────────────────────────────────────────────────────


def test_the_feature_is_named_capture_socket_mode() -> None:
    assert CAPTURE_SOCKET_MODE_FEATURE == "capture.socket_mode"


def test_health_lists_the_feature_when_the_sockets_are_shared_with_the_group() -> None:
    config = UtermServerConfig.model_validate({"pam": {"capture_socket_mode": 0o660}})
    assert server_features(config) == ["viewers.revoke.subject", "capture.socket_mode"]


def test_health_does_not_list_it_for_owner_only_sockets() -> None:
    assert server_features(UtermServerConfig()) == ["viewers.revoke.subject"]


def test_health_does_not_list_it_for_a_config_without_pam() -> None:
    assert server_features(None) == ["viewers.revoke.subject"]
