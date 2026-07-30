#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Bind-derived default for ``webhooks.allow_loopback_destinations``.

The effective loopback permission is::

    config.webhooks.allow_loopback_destinations or _is_loopback_host(config.server.host)

so a loopback-bound server (the default bind) accepts loopback webhook
destinations with no config at all, while a routable bind still refuses them
until the operator sets the key explicitly.

Everything else in the SSRF deny list (private / link-local / multicast /
unspecified / reserved / cloud-metadata) stays refused on *every* bind, with
and without the key — the flag must never re-open those.  The parametrized
``test_never_reopens_*`` cases assert that invariant; they pass both before and
after the change by design (they exist to catch a future implementation that
turns the key into a blanket "allow anything" switch).

Driven through the real ``create_server_app`` factory + HTTP route so the whole
config-to-behaviour path is under test, not a convenient seam.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from provide.uterm.server.app import create_server_app
from provide.uterm.server.config import config_from_mapping

ADMIN_H = {"X-Uterm-Principal": "admin-user", "X-Uterm-Role": "admin"}

# The exact text the SSRF guard raises. Asserted verbatim so a refusal that
# happens for some *other* reason (404 session, 403 authz, pydantic 422) cannot
# be mistaken for the guard firing.
GUARD_MESSAGE = "webhook url host is not allowed"

_LOOPBACK_URLS = (
    "http://127.0.0.1:9999/hook",
    "https://localhost/hook",
    "https://[::1]/hook",
)


def _config(*, host: str, allow_loopback: bool | None) -> Any:
    webhooks: dict[str, Any] = {} if allow_loopback is None else {"allow_loopback_destinations": allow_loopback}
    return config_from_mapping(
        {
            "server": {"host": host, "port": 8780},
            "auth": {
                "mode": "header",
                "header_mode_acknowledged": True,
                "worker_bearer_token": "test-bearer-token-32-chars-long-x",
                # Required by the header-mode non-loopback validator. The
                # TestClient's transport peer reports itself as "testclient".
                "trusted_proxy_ips": ["testclient"],
            },
            "webhooks": webhooks,
            "sessions": [
                {
                    "session_id": "s1",
                    "display_name": "S1",
                    "connector_type": "shell",
                    "auto_start": False,
                }
            ],
        }
    )


def _client(*, host: str, allow_loopback: bool | None = None) -> Iterator[TestClient]:
    with TestClient(create_server_app(_config(host=host, allow_loopback=allow_loopback))) as client:
        yield client


def _register(client: TestClient, url: str) -> Any:
    return client.post("/api/sessions/s1/webhooks", json={"url": url}, headers=ADMIN_H)


# ---------------------------------------------------------------------------
# Part 1 — loopback bind implies the loopback destination permission
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", _LOOPBACK_URLS)
def test_loopback_bind_allows_loopback_destination_with_no_key(url: str) -> None:
    """127.0.0.1 bind + no ``webhooks`` config at all → loopback registers."""
    for client in _client(host="127.0.0.1"):
        resp = _register(client, url)
        assert resp.status_code == 200, resp.text
        assert resp.json()["url"] == url


@pytest.mark.parametrize("host", ["localhost", "::1", " 127.0.0.1 ", "LOCALHOST"])
def test_every_loopback_bind_spelling_allows_loopback_destination(host: str) -> None:
    """The permission follows ``_is_loopback_host``, not a literal "127.0.0.1"."""
    for client in _client(host=host):
        resp = _register(client, "http://127.0.0.1:9999/hook")
        assert resp.status_code == 200, resp.text


def test_loopback_bind_with_key_false_still_allows_loopback_destination() -> None:
    """An explicit ``false`` cannot *subtract* the bind-derived permission.

    The key means "ALSO allow loopback on a routable bind"; it is not an
    opt-out. A config that spells out the schema default must behave exactly
    like one that omits it.
    """
    for client in _client(host="127.0.0.1", allow_loopback=False):
        resp = _register(client, "http://127.0.0.1:9999/hook")
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Part 2 — a routable bind still refuses loopback until opted in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", _LOOPBACK_URLS)
def test_routable_bind_refuses_loopback_destination_with_no_key(url: str) -> None:
    for client in _client(host="0.0.0.0"):
        resp = _register(client, url)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == GUARD_MESSAGE


@pytest.mark.parametrize("url", _LOOPBACK_URLS)
def test_routable_bind_allows_loopback_destination_when_key_set(url: str) -> None:
    for client in _client(host="0.0.0.0", allow_loopback=True):
        resp = _register(client, url)
        assert resp.status_code == 200, resp.text
        assert resp.json()["url"] == url


# ---------------------------------------------------------------------------
# "No way to re-open" — every non-loopback refusal survives the key on every bind
# ---------------------------------------------------------------------------

_EVERY_BIND = pytest.mark.parametrize(
    ("host", "allow_loopback"),
    [
        ("127.0.0.1", None),
        ("127.0.0.1", True),
        ("0.0.0.0", None),
        ("0.0.0.0", True),
    ],
)

# Literal destinations that must stay refused regardless of bind or key.
_NEVER_ALLOWED_LITERALS = (
    "http://169.254.169.254/latest/meta-data",  # link-local cloud metadata
    "https://169.254.1.1/hook",  # link-local
    "https://10.0.0.1/hook",  # private
    "https://172.16.0.1/hook",  # private
    "https://192.168.1.1/hook",  # private
    "http://100.100.100.200/latest/meta-data",  # Alibaba metadata
    "https://0.0.0.0/hook",  # unspecified
    "https://224.0.0.1/hook",  # multicast
    "https://240.0.0.1/hook",  # reserved (Class E)
    "https://metadata.google.internal/computeMetadata/v1/",  # named metadata host
    "https://[fd00:ec2::254]/hook",  # AWS IMDS over IPv6
    # Embedded-IPv4 IPv6 forms: each carries the v4 metadata address in its low
    # bits and reaches it on a NAT64/6to4 network. CPython's classifiers already
    # reject all of them (see the ``NOTE on embedded-IPv4`` block in
    # webhooks.py); pinned here so a future CPython relaxation is caught.
    "https://[64:ff9b::169.254.169.254]/hook",  # NAT64 well-known prefix
    "https://[::ffff:169.254.169.254]/hook",  # IPv4-mapped
    "https://[2002:a9fe:a9fe::]/hook",  # 6to4 wrapping 169.254.169.254
)


@_EVERY_BIND
@pytest.mark.parametrize("url", _NEVER_ALLOWED_LITERALS)
def test_never_reopens_literal_destination(host: str, allow_loopback: bool | None, url: str) -> None:
    for client in _client(host=host, allow_loopback=allow_loopback):
        resp = _register(client, url)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == GUARD_MESSAGE


@_EVERY_BIND
@pytest.mark.parametrize("resolved", ["10.1.2.3", "169.254.169.254", "192.168.0.7"])
def test_never_reopens_hostname_resolving_to_blocked_address(
    host: str, allow_loopback: bool | None, resolved: str
) -> None:
    """A DNS name is judged by what it resolves to, on every bind, key or not."""
    for client in _client(host=host, allow_loopback=allow_loopback):
        with patch(
            "provide.uterm.server.webhooks._resolve_hostname_sync",
            return_value=(resolved,),
        ):
            resp = _register(client, "https://attacker.example.com/hook")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == GUARD_MESSAGE


@_EVERY_BIND
@pytest.mark.parametrize("failure", [OSError("nope"), TimeoutError()])
def test_never_reopens_hostname_that_fails_to_resolve(
    host: str, allow_loopback: bool | None, failure: Exception
) -> None:
    """A name the resolver cannot answer is a refusal, never a pass."""
    for client in _client(host=host, allow_loopback=allow_loopback):
        with patch(
            "provide.uterm.server.webhooks._resolve_hostname_sync",
            side_effect=failure,
        ):
            resp = _register(client, "https://nxdomain.example.com/hook")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == "webhook url host could not be resolved"


@_EVERY_BIND
def test_never_reopens_hostname_with_empty_resolution(host: str, allow_loopback: bool | None) -> None:
    """An empty answer must not fall through the per-address loop as allowed."""
    for client in _client(host=host, allow_loopback=allow_loopback):
        with patch("provide.uterm.server.webhooks._resolve_hostname_sync", return_value=()):
            resp = _register(client, "https://empty.example.com/hook")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == "webhook url host could not be resolved"


@_EVERY_BIND
def test_public_destination_still_registers_on_every_bind(host: str, allow_loopback: bool | None) -> None:
    """Control case: the guard did not become a blanket deny."""
    for client in _client(host=host, allow_loopback=allow_loopback):
        with patch(
            "provide.uterm.server.webhooks._resolve_hostname_sync",
            return_value=("93.184.216.34",),
        ):
            resp = _register(client, "https://hooks.example.com/uterm")
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# The hostname-resolving-to-loopback case follows the same effective permission
# ---------------------------------------------------------------------------


def test_loopback_bind_allows_hostname_resolving_to_loopback() -> None:
    for client in _client(host="127.0.0.1"):
        with patch(
            "provide.uterm.server.webhooks._resolve_hostname_sync",
            return_value=("127.0.0.1",),
        ):
            resp = _register(client, "https://dev.example.com/hook")
        assert resp.status_code == 200, resp.text


def test_routable_bind_refuses_hostname_resolving_to_loopback() -> None:
    for client in _client(host="0.0.0.0"):
        with patch(
            "provide.uterm.server.webhooks._resolve_hostname_sync",
            return_value=("127.0.0.1",),
        ):
            resp = _register(client, "https://dev.example.com/hook")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == GUARD_MESSAGE
