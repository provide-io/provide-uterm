#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unit tests for RFB dial config + upstream factory (no live VNC required)."""

from __future__ import annotations

import socket
import ssl
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from provide.uterm.server import default_server_config
from provide.uterm.server.config_schema import GraphicalTargetConfig
from provide.uterm.server.graphical_routes import _config_to_definition, seed_graphical_targets
from provide.uterm.server.graphical_targets import (
    GraphicalTargetDefinition,
    InMemoryGraphicalTargetRegistry,
    system_scope,
)
from provide.uterm.server.vnc_upstream import (
    DEFAULT_CONNECT_TIMEOUT_S,
    RfbDialConfig,
    attach_vnc_upstream_factory,
    dial_config_from_target,
    make_vnc_upstream_factory,
    open_rfb_upstream,
    resolve_rfb_target,
)


def test_dial_config_plain_from_definition() -> None:
    target = GraphicalTargetDefinition(
        target_id="lab-plain",
        protocol="rfb",
        endpoint="127.0.0.1:5900",
    )
    dial = dial_config_from_target(target)
    assert dial is not None
    assert dial.host == "127.0.0.1"
    assert dial.port == 5900
    assert dial.tls is False
    assert dial.tls_insecure is False
    assert dial.connect_timeout_s == DEFAULT_CONNECT_TIMEOUT_S
    assert dial.endpoint_label() == "rfb://127.0.0.1:5900"


def test_dial_config_tls_from_config_map() -> None:
    target = GraphicalTargetDefinition(
        target_id="lab-tls",
        protocol="rfb",
        endpoint="127.0.0.1:5901",
        config={"tls": True, "tls_insecure": True, "connect_timeout_s": 3},
    )
    dial = dial_config_from_target(target)
    assert dial is not None
    assert dial.tls is True
    assert dial.tls_insecure is True
    assert dial.connect_timeout_s == 3.0
    assert dial.endpoint_label() == "rfbs://127.0.0.1:5901"


def test_dial_config_ssl_aliases() -> None:
    target = GraphicalTargetDefinition(
        target_id="lab-ssl",
        protocol="rfb",
        endpoint="vnc.lab:5901",
        config={"ssl": "yes", "ssl_insecure": "1"},
    )
    dial = dial_config_from_target(target)
    assert dial is not None
    assert dial.tls is True
    assert dial.tls_insecure is True


def test_dial_config_rfbs_scheme_before_normalize() -> None:
    target = GraphicalTargetDefinition(
        target_id="lab-rfbs",
        protocol="rfb",
        endpoint="rfbs://10.0.0.2:5901",
    )
    dial = dial_config_from_target(target)
    assert dial is not None
    assert dial.tls is True
    assert dial.host == "10.0.0.2"
    assert dial.port == 5901


def test_dial_config_rejects_non_rfb() -> None:
    target = GraphicalTargetDefinition(target_id="mem", protocol="memory")
    assert dial_config_from_target(target) is None


def test_seed_rfbs_sets_tls_config() -> None:
    cfg = GraphicalTargetConfig(
        target_id="lab-rfbs",
        tenant_id="lab",
        protocol="rfb",
        target_address="rfbs://127.0.0.1:5901",
        name="Lab TLS",
    )
    d = _config_to_definition(cfg)
    assert d.endpoint == "127.0.0.1:5901"
    assert d.config.get("tls") is True
    dial = dial_config_from_target(d)
    assert dial is not None
    assert dial.tls is True


def test_seed_explicit_tls_config_wins_over_scheme_default() -> None:
    # setdefault: if operator already set tls=false under config, keep it
    cfg = GraphicalTargetConfig(
        target_id="lab-x",
        protocol="rfb",
        target_address="rfbs://127.0.0.1:5901",
        config={"tls": False},
    )
    d = _config_to_definition(cfg)
    assert d.config.get("tls") is False


def test_open_rfb_upstream_plain_uses_create_connection() -> None:
    mock_sock = MagicMock(spec=socket.socket)
    mock_r = BytesIO(b"RFB 003.008\n")
    mock_w = BytesIO()
    mock_sock.makefile.side_effect = [mock_r, mock_w]

    def _connect(addr: tuple[str, int], timeout: float) -> socket.socket:
        assert addr == ("127.0.0.1", 5900)
        assert timeout == 5.0
        return mock_sock

    dial = RfbDialConfig(host="127.0.0.1", port=5900, target_id="t")
    r, w = open_rfb_upstream(dial, create_connection=_connect)
    assert r is mock_r
    assert w is mock_w
    assert mock_sock.makefile.call_count == 2
    mock_sock.settimeout.assert_called_with(None)


def test_open_rfb_upstream_tls_wraps_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = MagicMock(spec=socket.socket)
    tls_sock = MagicMock(spec=ssl.SSLSocket)
    mock_r = BytesIO(b"RFB 003.008\n")
    mock_w = BytesIO()
    tls_sock.makefile.side_effect = [mock_r, mock_w]

    ctx = MagicMock()
    ctx.wrap_socket.return_value = tls_sock

    def _fake_ssl_context(*, tls_insecure: bool) -> Any:
        assert tls_insecure is True
        return ctx

    monkeypatch.setattr("provide.uterm.server.vnc_upstream._ssl_context", _fake_ssl_context)

    def _connect(_addr: tuple[str, int], _timeout: float) -> socket.socket:
        return raw

    dial = RfbDialConfig(host="127.0.0.1", port=5901, tls=True, tls_insecure=True, target_id="tls")
    r, w = open_rfb_upstream(dial, create_connection=_connect)
    ctx.wrap_socket.assert_called_once()
    assert r is mock_r
    assert w is mock_w


def test_open_rfb_upstream_tls_verify_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, bool] = {}

    def _fake_ssl_context(*, tls_insecure: bool) -> Any:
        seen["insecure"] = tls_insecure
        ctx = MagicMock()
        sock = MagicMock()
        sock.makefile.side_effect = [BytesIO(), BytesIO()]
        ctx.wrap_socket.return_value = sock
        return ctx

    monkeypatch.setattr("provide.uterm.server.vnc_upstream._ssl_context", _fake_ssl_context)
    dial = RfbDialConfig(host="vnc.example", port=5901, tls=True, tls_insecure=False)
    open_rfb_upstream(dial, create_connection=lambda *_a, **_k: MagicMock())
    assert seen["insecure"] is False


def test_factory_missing_target_returns_none() -> None:
    reg = InMemoryGraphicalTargetRegistry()
    factory = make_vnc_upstream_factory(reg)
    assert factory("w1", None) is None
    assert factory("w1", "nope") is None


def test_factory_non_rfb_returns_none() -> None:
    reg = InMemoryGraphicalTargetRegistry()
    reg.add_static(GraphicalTargetDefinition(target_id="mem", protocol="memory", tenant_id="lab"))
    factory = make_vnc_upstream_factory(reg)
    assert factory("w1", "mem") is None


def test_factory_dial_failure_returns_none() -> None:
    reg = InMemoryGraphicalTargetRegistry()
    reg.add_static(GraphicalTargetDefinition(target_id="lab", protocol="rfb", endpoint="127.0.0.1:1", tenant_id="lab"))

    def _boom(*_a: object, **_k: object) -> socket.socket:
        raise ConnectionRefusedError("refused")

    factory = make_vnc_upstream_factory(reg, create_connection=_boom)
    assert factory("w1", "lab") is None


def test_factory_success_returns_streams() -> None:
    reg = InMemoryGraphicalTargetRegistry()
    reg.add_static(
        GraphicalTargetDefinition(target_id="lab", protocol="rfb", endpoint="127.0.0.1:5900", tenant_id="lab")
    )
    mock_sock = MagicMock(spec=socket.socket)
    mock_sock.makefile.side_effect = [BytesIO(b"x"), BytesIO()]

    factory = make_vnc_upstream_factory(reg, create_connection=lambda *_a, **_k: mock_sock)
    streams = factory("w1", "lab")
    assert streams is not None
    assert len(streams) == 2


def test_attach_sets_hub_attribute() -> None:
    hub = SimpleNamespace()
    reg = InMemoryGraphicalTargetRegistry()
    attach_vnc_upstream_factory(hub, reg)
    assert callable(hub.vnc_upstream_factory)


def test_resolve_rfb_target_from_seed() -> None:
    cfg = default_server_config()
    cfg.graphical_targets = [
        GraphicalTargetConfig(
            target_id="lab-vnc",
            tenant_id="lab",
            protocol="rfb",
            target_address="127.0.0.1:5900",
            enabled=True,
            config={"tls": False},
        )
    ]
    reg = seed_graphical_targets(cfg)
    got = resolve_rfb_target(reg, "lab-vnc")
    assert got is not None
    assert got.endpoint == "127.0.0.1:5900"
    assert reg.get(system_scope(), "lab-vnc") is not None


def test_ssl_context_insecure_disables_verify() -> None:
    from provide.uterm.server.vnc_upstream import _ssl_context

    ctx = _ssl_context(tls_insecure=True)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_ssl_context_secure_requires_verify() -> None:
    from provide.uterm.server.vnc_upstream import _ssl_context

    ctx = _ssl_context(tls_insecure=False)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
