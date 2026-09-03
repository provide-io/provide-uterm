#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for the reference server's authentication dependency.

``_require_authenticated`` is a closure inside ``create_server_app``, so every
mutant in it is attributed to the factory and nothing in the perimeter reached
it — ``factory.py`` is an eleven-line re-export shim that was on the mutation
perimeter while these 610 lines were not
(``docs/mutmut-survivors-triage.md`` Wave 10).

It is the front door. Five separate ways in are checked in a fixed order, each
short-circuiting the rest, and every one of them is a bypass if its guard is
weakened:

*The e2e test-mode admin.* Mints an admin principal for WebSockets with no
token at all, gated on an env var and on the connection being a WebSocket. If
that gate loosens to HTTP, or stops comparing the flag exactly, an unauthenticated
admin appears on a production server.

*The tunnel share principal* and *the tunnel worker principal*, both consulted
before JWT resolution so share links and per-session tunnel tokens are not
mis-rejected as anonymous.

*The worker bearer token.* Compared with ``secrets.compare_digest``, and only
on a WebSocket whose path starts with ``/ws/worker/``. Both narrowing operands
matter: the token is a full admin credential, so accepting it on a browser
socket or over HTTP widens it to the whole API.

*The refusal itself.* A WebSocket is refused with 401 **before** the upgrade.
Starlette's unaided refusal is a pre-accept close, which every ASGI server
reports as 403 — an authorization answer to an authentication failure, and a
different thing for a client to act on. The two surfaces also increment
different counters and log different lines; those are what an operator reads to
tell "nobody is logging in" from "the workers cannot connect".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse

from provide.uterm.server import create_server_app, default_server_config
from provide.uterm.server.app import factory_impl
from provide.uterm.server.auth import Principal

_WORKER_TOKEN = "uterm-test-worker-bearer-value-32-bytes"
_ENDPOINT = "/api/durability/capabilities"


def _app(*, worker_token: str | None = _WORKER_TOKEN) -> Any:
    config = default_server_config()
    config.auth.worker_bearer_token = worker_token
    return create_server_app(config, api_only=True)


def _anon(app: Any) -> TestClient:
    """A client with the conftest's auto-attached dev token removed."""
    client = TestClient(app)
    client.headers.pop("Authorization", None)
    return client


def _auth_metrics(app: Any) -> dict[str, int]:
    return {k: v for k, v in app.state.uterm_metrics.items() if k.startswith("auth_failures")}


def _principal(subject_id: str = "someone", role: str = "admin") -> Principal:
    return Principal(subject_id=subject_id, roles=frozenset({role}), scopes=frozenset({"*"}))


# ---------------------------------------------------------------------------
# The HTTP refusal
# ---------------------------------------------------------------------------


def test_an_unauthenticated_http_request_is_refused() -> None:
    app = _app()
    with _anon(app) as client:
        response = client.get(_ENDPOINT)

    assert response.status_code == 401
    assert response.json()["detail"] == "authentication required"


def test_an_authenticated_http_request_is_served() -> None:
    """The near side, so "always refuse" cannot pass."""
    app = _app()
    with TestClient(app) as client:
        assert client.get(_ENDPOINT).status_code == 200


def test_an_http_failure_increments_only_the_http_counter() -> None:
    """Two counters, and an operator reads them to tell the surfaces apart.

    Asserted together: a single shared name passes any test that checks only
    the one it expects to move.
    """
    app = _app()
    with _anon(app) as client:
        client.get(_ENDPOINT)

    assert _auth_metrics(app) == {"auth_failures_http_total": 1, "auth_failures_ws_total": 0}


def test_an_http_failure_names_its_surface_in_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted as an exact call — the surface is the whole content of the line."""
    app = _app()
    recorder = MagicMock()
    monkeypatch.setattr(factory_impl, "logger", recorder)
    with _anon(app) as client:
        client.get(_ENDPOINT)

    recorder.info.assert_called_once_with("authn_denied surface=http")


# ---------------------------------------------------------------------------
# The WebSocket refusal — 401 before the upgrade, not a 403 close
# ---------------------------------------------------------------------------


def test_an_unauthenticated_websocket_is_refused_with_401_before_the_upgrade() -> None:
    """The reason ``WebSocketAuthDenied`` exists at all.

    Starlette's own refusal closes before accept, which ASGI servers report as
    403. A client cannot tell "you are not logged in" from "you are not allowed"
    from that, and the Go and C# ports both answer 401 here.
    """
    app = _app()
    with _anon(app) as client, pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect("/ws/browser/w1/term"):
            pass

    assert denial.value.status_code == 401


def test_a_websocket_failure_increments_only_the_websocket_counter() -> None:
    app = _app()
    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/browser/w1/term"):
            pass

    assert _auth_metrics(app) == {"auth_failures_http_total": 0, "auth_failures_ws_total": 1}


def test_a_websocket_failure_names_its_surface_in_the_log(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app()
    recorder = MagicMock()
    monkeypatch.setattr(factory_impl, "logger", recorder)
    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/browser/w1/term"):
            pass

    recorder.info.assert_called_once_with("authn_denied surface=websocket")


# ---------------------------------------------------------------------------
# The e2e test-mode admin
# ---------------------------------------------------------------------------


def test_test_mode_admits_an_unauthenticated_websocket_as_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker is unregistered, so only an admin principal gets through.

    ``_resolve_browser_role`` fails closed on a session it has no visibility
    policy for: admin observes, everyone else is refused. Connecting at all
    therefore proves the minted principal carries the admin role, not merely
    that some principal was minted.
    """
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    app = _app()

    with _anon(app) as client, client.websocket_connect("/ws/browser/w1/term"):
        pass


def test_test_mode_does_not_admit_an_http_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gated on the connection being a WebSocket; widening it opens the API."""
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    app = _app()

    with _anon(app) as client:
        assert client.get(_ENDPOINT).status_code == 401


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_test_mode_admits_nothing_unless_the_flag_is_exactly_one(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Never default-on: a truthy-looking value is not the flag."""
    monkeypatch.setenv("UTERM_TEST_MODE", value)
    app = _app()

    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/browser/w1/term"):
            pass


# ---------------------------------------------------------------------------
# The worker bearer token
# ---------------------------------------------------------------------------


def test_a_worker_bearer_token_admits_a_worker_websocket() -> None:
    """Authentication passes: the socket is accepted, then the route takes over.

    A refusal arrives as ``WebSocketDenialResponse`` (401 pre-upgrade); anything
    after the accept is the worker protocol's business, not this dependency's.
    """
    app = _app()
    with _anon(app) as client:
        try:
            with client.websocket_connect("/ws/worker/w1/term", headers={"Authorization": f"Bearer {_WORKER_TOKEN}"}):
                pass
        except WebSocketDenialResponse as denial:  # pragma: no cover - the failure we are pinning
            pytest.fail(f"a valid worker token was refused with {denial.status_code}")
        except Exception:
            pass  # accepted, then closed by the worker route


def test_a_wrong_worker_bearer_token_is_refused() -> None:
    """``compare_digest`` against the configured value — not any bearer at all."""
    app = _app()
    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/worker/w1/term", headers={"Authorization": "Bearer wrong-token-value"}):
            pass


def test_the_worker_token_is_not_accepted_on_a_browser_socket() -> None:
    """The ``/ws/worker/`` path guard. It is a full admin credential."""
    app = _app()
    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/browser/w1/term", headers={"Authorization": f"Bearer {_WORKER_TOKEN}"}):
            pass


def test_the_worker_token_is_not_accepted_over_http() -> None:
    """The WebSocket operand — otherwise the token authenticates the whole API."""
    app = _app()
    with _anon(app) as client:
        response = client.get(_ENDPOINT, headers={"Authorization": f"Bearer {_WORKER_TOKEN}"})

    assert response.status_code == 401


def test_no_configured_worker_token_means_no_bypass() -> None:
    """The first operand: an unset token must not make an empty compare succeed."""
    app = _app(worker_token=None)
    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/worker/w1/term", headers={"Authorization": "Bearer "}):
            pass


# ---------------------------------------------------------------------------
# Tunnel share and tunnel worker principals
# ---------------------------------------------------------------------------


def test_a_tunnel_share_principal_authenticates_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Share links reach the inspector page; without this they 401."""
    monkeypatch.setattr(factory_impl, "resolve_tunnel_share_principal", lambda *a, **k: _principal("shared"))
    app = _app()

    with _anon(app) as client:
        assert client.get(_ENDPOINT).status_code == 200


def test_the_share_resolver_is_given_this_servers_config_and_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The patterns are what decide which paths a share cookie may authenticate.

    Handing the resolver no patterns, or someone else's config, makes it answer
    a different question than the one this server asked.
    """
    seen: dict[str, Any] = {}

    def _record(connection: Any, *, config: Any, patterns: Any) -> Principal | None:
        seen["config"], seen["patterns"] = config, patterns
        return None

    monkeypatch.setattr(factory_impl, "resolve_tunnel_share_principal", _record)
    app = _app()
    with _anon(app) as client:
        client.get(_ENDPOINT)

    assert seen["config"] is app.state.uterm_config
    assert seen["patterns"] is factory_impl._SHARE_SESSION_PATTERNS


def test_a_tunnel_worker_principal_authenticates_a_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-session /tunnel/{id} tokens are not JWTs and would be rejected as anonymous."""
    monkeypatch.setattr(factory_impl, "resolve_tunnel_ws_worker_principal", lambda *a, **k: _principal("tunnel"))
    app = _app()

    with _anon(app) as client, client.websocket_connect("/ws/browser/w1/term"):
        pass


def test_the_tunnel_worker_resolver_is_not_consulted_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """It reads WebSocket scope; the guard is what keeps HTTP out of it."""
    calls: list[Any] = []
    monkeypatch.setattr(
        factory_impl,
        "resolve_tunnel_ws_worker_principal",
        lambda *a, **k: calls.append(a) or None,
    )
    app = _app()
    with _anon(app) as client:
        client.get(_ENDPOINT)

    assert calls == []


# ---------------------------------------------------------------------------
# The configured identity provider
# ---------------------------------------------------------------------------


def test_a_configured_idp_that_recognises_nobody_yields_an_anonymous_viewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` from the IDP is not an error — it is an unauthenticated caller.

    Returning it as a principal with no subject would authenticate everyone the
    IDP declined.
    """

    class _RecognisesNobody:
        async def resolve_principal(self, _connection: Any) -> Principal | None:
            return None

    monkeypatch.setattr(factory_impl, "build_identity_provider", lambda *a, **k: _RecognisesNobody())
    app = _app()

    with _anon(app) as client:
        assert client.get(_ENDPOINT).status_code == 401


def test_a_configured_idp_principal_is_the_one_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other arm, so "always anonymous" cannot pass."""

    class _RecognisesEveryone:
        async def resolve_principal(self, _connection: Any) -> Principal | None:
            return _principal("from-idp")

    monkeypatch.setattr(factory_impl, "build_identity_provider", lambda *a, **k: _RecognisesEveryone())
    app = _app()

    with _anon(app) as client:
        assert client.get(_ENDPOINT).status_code == 200


# ---------------------------------------------------------------------------
# Exactly which principal each way in mints
# ---------------------------------------------------------------------------


@pytest.fixture()
def minted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every ``Principal(...)`` the factory constructs, with its exact fields.

    The status code alone cannot see these: a worker principal built with no
    roles, or with the wrong subject, still authenticates the socket. The
    identity IS the authorization input for everything downstream, so it is
    asserted field by field.
    """
    made: list[dict[str, Any]] = []
    real = factory_impl.Principal

    def _record(**kwargs: Any) -> Any:
        made.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(factory_impl, "Principal", _record)
    return made


def test_a_worker_token_mints_a_full_admin_named_worker(minted: list[dict[str, Any]]) -> None:
    """``worker`` with admin role and the wildcard scope — an audit trail identity.

    A blank subject makes every worker anonymous in the audit log; missing roles
    or scopes make the token authenticate but authorize nothing, which fails the
    worker at its first hub operation instead of at the door.
    """
    app = _app()
    with _anon(app) as client:
        try:
            with client.websocket_connect("/ws/worker/w1/term", headers={"Authorization": f"Bearer {_WORKER_TOKEN}"}):
                pass
        except Exception:
            pass

    assert {
        "subject_id": "worker",
        "roles": frozenset({"admin"}),
        "scopes": frozenset({"*"}),
    } in minted


def test_test_mode_mints_a_named_admin_rather_than_an_unnamed_one(
    monkeypatch: pytest.MonkeyPatch, minted: list[dict[str, Any]]
) -> None:
    """``test-admin`` is deliberately distinguishable from a real admin in the log."""
    monkeypatch.setenv("UTERM_TEST_MODE", "1")
    app = _app()

    with _anon(app) as client, client.websocket_connect("/ws/browser/w1/term"):
        pass

    assert {
        "subject_id": "test-admin",
        "roles": frozenset({"admin"}),
        "scopes": frozenset({"*"}),
    } in minted


def test_an_unrecognised_caller_is_a_scopeless_anonymous_viewer(
    monkeypatch: pytest.MonkeyPatch, minted: list[dict[str, Any]]
) -> None:
    """``anonymous`` is the sentinel the 401 check compares against.

    Any other subject id makes an unrecognised caller pass that check, and the
    empty scope set is what stops the fallback principal from carrying
    authority it was never granted.
    """

    class _RecognisesNobody:
        async def resolve_principal(self, _connection: Any) -> Principal | None:
            return None

    monkeypatch.setattr(factory_impl, "build_identity_provider", lambda *a, **k: _RecognisesNobody())
    app = _app()

    with _anon(app) as client:
        assert client.get(_ENDPOINT).status_code == 401

    assert {
        "subject_id": "anonymous",
        "roles": frozenset({"viewer"}),
        "scopes": frozenset(),
    } in minted


# ---------------------------------------------------------------------------
# The refusals carry their reason
# ---------------------------------------------------------------------------


def test_the_websocket_refusal_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body is what a browser shows; a blank reason is an unexplained failure."""
    app = _app()
    with _anon(app) as client, pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect("/ws/browser/w1/term"):
            pass

    assert denial.value.json() == {"detail": "authentication required"}


# ---------------------------------------------------------------------------
# Which resolver the local provider is asked, and with what
# ---------------------------------------------------------------------------


def test_an_http_request_is_resolved_by_the_http_resolver_with_this_auth_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WS and HTTP have separate resolvers; the auth config is what they read."""
    seen: list[Any] = []

    async def _record(connection: Any, auth: Any) -> Principal:
        seen.append((connection, auth))
        return _principal("admin")

    monkeypatch.setattr(factory_impl, "resolve_http_principal", _record)
    app = _app()
    with _anon(app) as client:
        assert client.get(_ENDPOINT).status_code == 200

    assert len(seen) == 1
    assert seen[0][0] is not None
    assert seen[0][1] is app.state.uterm_config.auth


def test_a_websocket_is_resolved_by_the_websocket_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handing a WebSocket to the HTTP resolver reads headers that are not there."""
    seen: list[Any] = []

    async def _record(connection: Any, auth: Any) -> Principal:
        seen.append((connection, auth))
        return _principal("admin")

    monkeypatch.setattr(factory_impl, "resolve_ws_principal", _record)
    app = _app()
    with _anon(app) as client, client.websocket_connect("/ws/browser/w1/term"):
        pass

    assert seen
    assert seen[0][1] is app.state.uterm_config.auth


def test_the_configured_idp_is_given_the_actual_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider handed ``None`` cannot read a header, a cookie or a peer address.

    It would then decline every caller for the same reason it declines a real
    anonymous one, so the status code is identical and only the argument
    distinguishes them.
    """
    seen: list[Any] = []

    class _NeedsTheConnection:
        async def resolve_principal(self, connection: Any) -> Principal | None:
            seen.append(connection)
            return _principal("admin") if connection is not None else None

    monkeypatch.setattr(factory_impl, "build_identity_provider", lambda *a, **k: _NeedsTheConnection())
    app = _app()

    with _anon(app) as client:
        assert client.get(_ENDPOINT).status_code == 200

    assert seen and seen[0] is not None


# ---------------------------------------------------------------------------
# The resolved principal has to be INSTALLED, not merely computed
# ---------------------------------------------------------------------------
#
# Each of the ways in ends by writing the principal onto
# ``connection.state.uterm_principal`` and returning. Dropping that write raises
# nothing and refuses nobody -- the dependency still returns, the request still
# succeeds. It is only observable further down, where something reads the
# principal back and gets a DIFFERENT answer by re-resolving.
#
# So these connect with the conftest's dev token attached (an admin), and have
# the short-circuit install a VIEWER. Installed, the browser is refused an
# unregistered worker; dropped, the fallback re-resolves the dev-token admin and
# the socket connects. Same request, opposite outcome.


def _viewer(subject_id: str) -> Principal:
    return Principal(subject_id=subject_id, roles=frozenset({"viewer"}), scopes=frozenset())


def _first_frame(client: TestClient) -> dict[str, Any]:
    """The first thing the browser socket is sent.

    A refused viewer is ACCEPTED and then closed with 1008 (that refusal is a
    ``WebSocketException`` inside the role resolver, not a pre-upgrade denial),
    while an admin is registered and sent its startup frames. The two are
    distinguishable on the first message alone.
    """
    with client.websocket_connect("/ws/browser/w1/term") as ws:
        return dict(ws.receive())


def test_a_share_principal_is_installed_on_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Computing it and dropping it silently restores the caller's own identity."""
    monkeypatch.setattr(factory_impl, "resolve_tunnel_share_principal", lambda *a, **k: _viewer("shared"))
    app = _app()

    with TestClient(app) as client:
        frame = _first_frame(client)

    assert (frame["type"], frame["code"]) == ("websocket.close", 1008)


def test_a_tunnel_worker_principal_is_installed_on_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory_impl, "resolve_tunnel_ws_worker_principal", lambda *a, **k: _viewer("tunnel-worker"))
    app = _app()

    with TestClient(app) as client:
        frame = _first_frame(client)

    assert (frame["type"], frame["code"]) == ("websocket.close", 1008)


def test_the_resolved_principal_is_installed_for_a_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary path: what was resolved is what the socket carries onward.

    Dropping the write raises nothing -- the anonymous check still runs against
    the local variable, so the request is not refused. It is only visible
    downstream, where ``_resolve_browser_role`` finds no principal and resolves
    the socket a SECOND time.

    So the resolver answers differently on its second call. Kept, the browser is
    the admin that was resolved first and connects; dropped, the re-resolution
    returns a viewer and an unregistered worker refuses it.
    """
    calls: list[int] = []

    async def _admin_then_viewer(_connection: Any, _auth: Any) -> Principal:
        calls.append(1)
        return _principal("admin") if len(calls) == 1 else _viewer("re-resolved")

    monkeypatch.setattr(factory_impl, "resolve_ws_principal", _admin_then_viewer)
    app = _app()

    with TestClient(app) as client:
        frame = _first_frame(client)

    assert frame["type"] != "websocket.close", "the resolved admin was not carried onward"
    assert len(calls) == 1, "the socket was resolved twice, so the first answer was discarded"


def test_the_websocket_resolver_is_given_the_socket_it_is_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver handed nothing reads no headers and recognises nobody."""
    seen: list[Any] = []

    async def _record(connection: Any, auth: Any) -> Principal:
        seen.append(connection)
        return _principal("admin") if connection is not None else _viewer("nobody")

    monkeypatch.setattr(factory_impl, "resolve_ws_principal", _record)
    app = _app()

    with _anon(app) as client, client.websocket_connect("/ws/browser/w1/term"):
        pass

    assert seen and seen[0] is not None


def test_the_tunnel_worker_resolver_is_given_this_servers_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads tunnel token state out of the config; another one answers for
    another server."""
    seen: list[Any] = []

    def _record(_connection: Any, *, config: Any) -> Principal | None:
        seen.append(config)
        return None

    monkeypatch.setattr(factory_impl, "resolve_tunnel_ws_worker_principal", _record)
    app = _app()

    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/browser/w1/term"):
            pass

    assert seen and seen[0] is app.state.uterm_config


def test_a_missing_bearer_never_matches_the_configured_worker_token() -> None:
    """The ``or ""`` fallback must not become a value the comparison can match.

    ``extract_bearer_token`` returns None when there is no header, so the
    fallback is what gets compared. Any non-empty fallback is a token an
    unauthenticated caller supplies for free — and the config accepts short
    tokens, so this is reachable rather than theoretical. The comparison here
    is the whole worker admission check.
    """
    app = _app(worker_token="XXXX")

    with _anon(app) as client, pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/worker/w1/term"):
            pass
