#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Targeted unit tests covering single-line gaps surfaced by the coverage
report (the "easy wins" set).

Each test docstring names the file:line it covers so future report-vs-test
correlation is obvious.
"""

from __future__ import annotations

import logging

import pytest

# ---------------------------------------------------------------------------
# bridge/hub/core.py:180 — set_worker_hello_mode rejects invalid input mode
# ---------------------------------------------------------------------------


async def test_set_worker_hello_mode_rejects_invalid_mode() -> None:
    from provide.uterm.server.bridge.hub import TermHub

    hub = TermHub()
    with pytest.raises(ValueError, match="invalid input mode"):
        await hub.set_worker_hello_mode("nobody", "bogus-mode")


# ---------------------------------------------------------------------------
# bridge/hub/connections.py:205 — legacy protocol_version warning log
# ---------------------------------------------------------------------------


async def test_worker_hello_logs_warning_for_legacy_protocol(caplog: pytest.LogCaptureFixture) -> None:
    from unittest.mock import AsyncMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.models import WorkerTermState

    hub = TermHub()
    hub._workers["w1"] = WorkerTermState(worker_ws=AsyncMock())
    # Phase 6 of refactor #16: log lives on the new ConnectionManager module.
    caplog.set_level(logging.WARNING, logger="provide.uterm.server.bridge.hub.connection")
    await hub.set_worker_hello("w1", "open", protocol_version=0)
    assert any("worker_hello_legacy_protocol" in r.getMessage() for r in caplog.records), (
        "set_worker_hello with protocol_version<1 must log worker_hello_legacy_protocol warning"
    )


# ---------------------------------------------------------------------------
# bridge/hub/semantics.py:16 — CommandSplitter.split("") -> []
# ---------------------------------------------------------------------------


def test_validate_webhook_url_rejects_invalid_scheme_and_metadata_host() -> None:
    from provide.uterm.server.webhooks import validate_webhook_url

    with pytest.raises(ValueError, match="must use http or https"):
        validate_webhook_url("ftp://example.com/hook")
    with pytest.raises(ValueError, match="must include a host"):
        validate_webhook_url("http:///hook")
    with pytest.raises(ValueError, match="host is not allowed"):
        validate_webhook_url("http://metadata.google.internal/hook")
    with pytest.raises(ValueError, match="host is not allowed"):
        validate_webhook_url("http://localhost/hook")
    # ``allow_loopback_destinations`` lets localhost through.
    assert validate_webhook_url("http://localhost/hook", allow_loopback_destinations=True) == "http://localhost/hook"


def test_validate_webhook_pattern_rejects_non_string_input() -> None:
    from provide.uterm.server.webhooks import validate_webhook_pattern

    with pytest.raises(ValueError, match="pattern must be a string"):
        validate_webhook_pattern(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# server/webhooks.py:299-300, 311-326, 331-332 — delivery URL allowlist edges
# ---------------------------------------------------------------------------


async def test_delivery_url_allowed_rejects_unparseable_and_disallowed() -> None:
    from provide.uterm.server.webhooks import _delivery_url_allowed

    async def _no_resolve(_host: str) -> tuple[str, ...]:
        return ()

    # urlparse only raises on rare malformed brackets; force the rejection
    # path with a value the validator considers invalid (no host).
    assert await _delivery_url_allowed("http:///nohost", _no_resolve) is False
    # Non-http(s) scheme
    assert await _delivery_url_allowed("ftp://example/", _no_resolve) is False

    # Resolver raises -> False
    async def _boom(_host: str) -> tuple[str, ...]:
        raise OSError("dns down")

    assert await _delivery_url_allowed("http://example/", _boom) is False
    # Resolver returns no addresses -> False
    assert await _delivery_url_allowed("http://example/", _no_resolve) is False


async def test_resolve_host_returns_addresses() -> None:
    from provide.uterm.server.webhooks import _resolve_host

    addrs = await _resolve_host("localhost")
    # At least one of the loopback addresses must be in the result.
    assert any(a.startswith("127.") or a == "::1" for a in addrs)


# ---------------------------------------------------------------------------
# bridge/hub/event_bus.py — small parser / filter gaps
# ---------------------------------------------------------------------------


async def test_event_bus_filters_non_string_screen_via_coercion() -> None:
    """event_bus.py:115-116 — pattern filter coerces non-string screen to str."""
    import asyncio as _asyncio
    import re as _re

    from provide.uterm.server.bridge.hub.event_bus import EventBus, _Subscription

    bus = EventBus()
    queue: _asyncio.Queue = _asyncio.Queue()
    sub = _Subscription(sub_id="s1", worker_id="w1", queue=queue, event_types=None, pattern=_re.compile(r"123"))
    # screen is a list (non-str); the filter must coerce via str(...) and match.
    bus._deliver(sub, "w1", {"type": "snapshot", "data": {"screen": ["123 found"]}, "seq": 1})
    assert queue.qsize() == 1


def test_event_bus_pattern_safety_char_class_and_counted_quantifier() -> None:
    """event_bus.py:274-275, 300, 310-317 — pattern safety walks [...] and {N,M}."""
    from provide.uterm.server.bridge.hub.event_bus import _compile_pattern

    # Character class — exercises in_class branch (274-275)
    assert _compile_pattern("[abc]+def") is not None
    # Counted quantifier `{2}` — exercises 300 + _looks_like_counted_quantifier (310-317)
    assert _compile_pattern("a{2}") is not None
    # Counted quantifier `{2,5}` — comma branch
    assert _compile_pattern("a{2,5}") is not None
    # Counted quantifier with empty upper `{2,}` — comma branch with empty right
    assert _compile_pattern("a{2,}") is not None
    # `{not_a_number}` — body is not all digits; treated as literal, returns False
    assert _compile_pattern("a{abc}") is not None
    # `{` with no matching `}` — returns False, treated as literal
    assert _compile_pattern("a{") is not None
    # `{}` empty body — returns False
    assert _compile_pattern("a{}") is not None


# ---------------------------------------------------------------------------
# bridge/hub/state.py — small property + role-mapping gaps
# ---------------------------------------------------------------------------


def test_state_event_bus_setter_assigns_value() -> None:
    """state.py:54 — event_bus.setter writes through to _event_bus."""
    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.hub.event_bus import EventBus

    hub = TermHub()
    bus = EventBus()
    hub.event_bus = bus
    assert hub._event_bus is bus
    hub.event_bus = None
    assert hub._event_bus is None


async def test_state_prepare_policy_context_role_resolution() -> None:
    """state.py:188-194, 218-225 — claims-driven role mapping (delegate_roles=False)."""
    from unittest.mock import AsyncMock, MagicMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.identity import Principal

    # delegate_roles=False: claims-driven role mapping.
    hub = TermHub(delegate_roles=False)
    ws = AsyncMock()

    # admin claim -> admin role
    ws.state = MagicMock(uterm_principal=Principal(subject_id="a", roles=frozenset(), claims={"admin": True}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "admin"

    # operator claim -> operator role (covers 220-221)
    ws.state = MagicMock(uterm_principal=Principal(subject_id="o", roles=frozenset(), claims={"operator": True}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "operator"

    # No claim -> viewer fallback (covers 224 + 194)
    ws.state = MagicMock(uterm_principal=Principal(subject_id="v", roles=frozenset(), claims={}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "viewer"


async def test_state_prepare_policy_context_delegated_roles_empty_falls_back_to_viewer() -> None:
    """state.py:214 — delegate_roles=True with empty principal.roles -> viewer."""
    from unittest.mock import AsyncMock, MagicMock

    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.bridge.identity import Principal

    hub = TermHub(delegate_roles=True)
    ws = AsyncMock()
    ws.state = MagicMock(uterm_principal=Principal(subject_id="x", roles=frozenset(), claims={}))
    ctx = await hub.prepare_policy_context(ws, "w1", action="test")
    assert ctx.role == "viewer"


# ---------------------------------------------------------------------------
# server/app/factory.py:414 — _resolve_browser_role returns "operator" when
# no session is registered AND the principal has an operator (but not admin)
# role claim.
# ---------------------------------------------------------------------------


async def test_resolve_browser_role_no_session_operator_principal_returns_operator() -> None:
    from types import SimpleNamespace

    from provide.uterm.server import create_server_app, default_server_config
    from provide.uterm.server.bridge.identity import Principal

    cfg = default_server_config()
    cfg.auth.mode = "header"
    cfg.auth.header_mode_acknowledged = True
    cfg.auth.worker_bearer_token = "test-bearer-token-32-chars-long-x"
    app = create_server_app(cfg)
    hub = app.state.uterm_hub

    mock_ws = SimpleNamespace(
        state=SimpleNamespace(uterm_principal=Principal(subject_id="op", roles=frozenset({"operator"}))),
        headers={},
        cookies={},
        scope={"type": "websocket", "headers": []},
    )
    role = await hub._resolve_browser_role(mock_ws, "no-such-session")
    assert role == "operator"


# ---------------------------------------------------------------------------
# bridge/hub/redaction.py:39-41, 51 — StreamRedactor ignores invalid regex
# rules and short-circuits when no pattern compiled.
# ---------------------------------------------------------------------------


def test_redaction_engine_ignores_invalid_pattern_and_passes_data_when_empty() -> None:
    from provide.uterm.server.bridge.hub.ext import RedactionRule
    from provide.uterm.server.bridge.hub.redaction import StreamRedactor

    # Only an invalid regex -> no compiled pattern -> redact() returns input as-is.
    engine = StreamRedactor([RedactionRule(pattern="[unterminated", replacement="X")])
    assert engine.redact("hello world") == "hello world"

    # Valid + invalid mixed -> only the valid one compiles.
    engine = StreamRedactor(
        [
            RedactionRule(pattern="[unterminated", replacement="X"),
            RedactionRule(pattern=r"\d+", replacement="###"),
        ]
    )
    assert engine.redact("a1 b22 c") == "a### b### c"


# ---------------------------------------------------------------------------
# bridge/fanout/_controller.py:226, 238 — release_approved_command returns
# None for unknown request_id and for revoked groups.
# ---------------------------------------------------------------------------


async def test_fanout_release_approved_command_returns_none_for_unknown_request() -> None:
    from unittest.mock import MagicMock

    from provide.uterm.server.bridge.fanout._controller import FanOutController

    ctrl = FanOutController(hub=MagicMock(), fanout_policy_gate=MagicMock())
    # No pending approval registered -> None (line 226).
    assert await ctrl.release_approved_command("missing-id") is None


# ---------------------------------------------------------------------------
# server/runtime.py:429 — `outcome == "cancelled"` break exits the run loop.
# ---------------------------------------------------------------------------


async def test_classify_run_error_cancelled() -> None:
    import asyncio as _asyncio

    from provide.uterm.server.runtime import _classify_run_error

    assert _classify_run_error(_asyncio.CancelledError()) == "cancelled"


# ---------------------------------------------------------------------------
# gateway/_ssh_handler.py:169-182 — _openssh_blob_for_key handles None,
# missing methods, raising methods, and str/bytes return types.
# ---------------------------------------------------------------------------


def test_openssh_blob_for_key_handles_all_branches() -> None:
    from provide.uterm.gateway._ssh_handler import _openssh_blob_for_key

    # None -> empty bytes (line 170)
    assert _openssh_blob_for_key(None) == b""

    # No matching attribute -> empty bytes
    assert _openssh_blob_for_key(object()) == b""

    # Attribute exists but not callable -> skipped
    class _NonCallable:
        export_public_key = "not-callable"

    assert _openssh_blob_for_key(_NonCallable()) == b""

    # Method raises -> continue to next attr (line 176-177)
    class _Raises:
        def export_public_key(self) -> bytes:
            raise RuntimeError("nope")

        def public_data(self) -> bytes:
            return b"valid-blob"

    assert _openssh_blob_for_key(_Raises()) == b"valid-blob"

    # str return type -> ASCII-encoded (line 180-181)
    class _StrReturn:
        def export_public_key(self) -> str:
            return "ascii-blob"

    assert _openssh_blob_for_key(_StrReturn()) == b"ascii-blob"

    # Method exists but returns neither str nor bytes -> falls through to next attr
    class _BadReturnThenValid:
        def export_public_key(self) -> object:
            return 12345  # not str/bytes — skip

        def public_data(self) -> bytes:
            return b"fallback-blob"

    assert _openssh_blob_for_key(_BadReturnThenValid()) == b"fallback-blob"


# ---------------------------------------------------------------------------
# gateway/_ssh_handler.py:187-196 — _fingerprint_for_key handles None,
# missing get_fingerprint, and exceptions.
# ---------------------------------------------------------------------------
