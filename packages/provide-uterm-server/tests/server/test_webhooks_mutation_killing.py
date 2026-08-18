#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation-killing tests for server/webhooks.py — the SSRF guard + delivery.

This is the dedicated mutmut-binding suite for webhooks.py (wired into
[tool.mutmut].pytest_add_cli_args_test_selection). Its defining property: it NEVER performs real network
I/O. An autouse fixture replaces ``socket.getaddrinfo`` with a MagicMock and every
delivery test patches the httpx2 client + ``asyncio.sleep``. That matters twice:

1. Correctness under mutation: mutmut forks a worker per mutant. The 7
   ``test_webhooks_part*.py`` coverage suites call the REAL resolver at
   registration time (``register("...example.com...")`` → ``_resolve_hostname_sync``
   → ``socket.getaddrinfo``); a mutant that perturbs the resolver/delivery args then
   drives the C resolver (or real httpx2 + real sleeps) in a forked child, which
   segfaults/times out the worker (the measured 307/406-mutant crash that deferred
   this file). Keeping all egress mocked keeps the real C resolver out of the child.
2. Killability: with getaddrinfo a MagicMock and the logger/guard/client patched we
   assert exact call args, so the resolver/log/delivery mutants become killable
   instead of merely crashing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server.webhooks import (
    _DELIVER_TIMEOUT_S,
    _MAX_BLOCKED_DELIVERIES,
    _REGISTER_DNS_TIMEOUT_S,
    WebhookConfig,
    WebhookManager,
    _address_allowed,
    _delivery_url_allowed,
    _delivery_url_is_loopback_only,
    _literal_or_resolved_addresses,
    _resolve_host,
    _resolve_hostname_sync,
    _tunnel_share_active,
    validate_webhook_pattern,
    validate_webhook_url,
)

# A safe, routable public IP the SSRF guard must ALLOW.
_PUBLIC_IP = "93.184.216.34"
_URL = "https://example.com/hook"


def _addrinfo(*ips: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    """Build getaddrinfo-shaped 5-tuples: (family, type, proto, canon, (ip, port))."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


def _resp(*, success: bool = True, status: int = 200) -> MagicMock:
    r = MagicMock(name="response")
    r.is_success = success
    r.status_code = status
    return r


@pytest.fixture(autouse=True)
def mock_getaddrinfo() -> Any:
    """Replace the real C resolver for EVERY test in this module."""
    fake = MagicMock(name="getaddrinfo", return_value=_addrinfo(_PUBLIC_IP))
    with patch("socket.getaddrinfo", fake):
        yield fake


@pytest.fixture(autouse=True)
def mock_httpx_default() -> Any:
    """No test in this module may perform real HTTP. A mutant that defeats the SSRF
    guard (``if not await _delivery_url_allowed`` → ``if await``) or the delivery-loop
    sentinel (``if item is None`` → ``is not None``) would otherwise fall through to a
    real ``httpx2.AsyncClient.post`` — and a real socket connect in a mutmut-forked
    child segfaults the worker. This blanket default keeps every such path mocked; the
    ``denv`` fixture re-patches it with a controllable client where delivery is asserted."""
    client = MagicMock(name="AsyncClient-default")
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=_resp(success=True))
    with patch("httpx2.AsyncClient", MagicMock(return_value=client)):
        yield


def _make_manager(resolved_ips: tuple[str, ...] = (_PUBLIC_IP,), **kwargs: Any) -> WebhookManager:
    return WebhookManager(resolver=lambda _hostname: resolved_ips, **kwargs)


def _make_event(event_type: str = "snapshot", screen: str = "$ test") -> dict[str, Any]:
    return {"type": event_type, "seq": 1, "ts": time.time(), "data": {"screen": screen}}


def _cfg(secret: str | None = None, webhook_id: str = "wh1", session_id: str = "s1") -> WebhookConfig:
    return WebhookConfig(
        webhook_id=webhook_id,
        session_id=session_id,
        url=_URL,
        event_types=None,
        pattern=None,
        secret=secret,
    )


# ===========================================================================
# _resolve_hostname_sync — the blocking stdlib resolver wrapper
# ===========================================================================


class TestResolveHostnameSync:
    def test_calls_getaddrinfo_with_exact_args(self, mock_getaddrinfo: MagicMock) -> None:
        _resolve_hostname_sync("example.com")
        mock_getaddrinfo.assert_called_once_with("example.com", None, type=socket.SOCK_STREAM)

    def test_returns_unique_ip_strings_from_sockaddr_index(self, mock_getaddrinfo: MagicMock) -> None:
        mock_getaddrinfo.return_value = _addrinfo("1.2.3.4", "1.2.3.4", "5.6.7.8")
        result = _resolve_hostname_sync("example.com")
        assert set(result) == {"1.2.3.4", "5.6.7.8"}
        assert len(result) == 2  # deduped

    def test_sets_short_timeout_then_restores_previous(self, mock_getaddrinfo: MagicMock) -> None:
        seen: list[float | None] = []
        mock_getaddrinfo.side_effect = lambda *a, **k: (seen.append(socket.getdefaulttimeout()), _addrinfo(_PUBLIC_IP))[
            1
        ]
        socket.setdefaulttimeout(37.0)
        try:
            _resolve_hostname_sync("example.com")
            assert seen == [_REGISTER_DNS_TIMEOUT_S], "short timeout must be active during getaddrinfo"
            assert socket.getdefaulttimeout() == 37.0, "previous timeout must be restored"
        finally:
            socket.setdefaulttimeout(None)

    def test_restores_timeout_even_when_getaddrinfo_raises(self, mock_getaddrinfo: MagicMock) -> None:
        mock_getaddrinfo.side_effect = socket.gaierror("boom")
        socket.setdefaulttimeout(11.0)
        try:
            with pytest.raises(socket.gaierror):
                _resolve_hostname_sync("example.com")
            assert socket.getdefaulttimeout() == 11.0
        finally:
            socket.setdefaulttimeout(None)


class TestResolveHostAsync:
    async def test_resolves_via_to_thread_with_exact_args(self, mock_getaddrinfo: MagicMock) -> None:
        mock_getaddrinfo.return_value = _addrinfo("8.8.8.8", "8.8.8.8")
        result = await _resolve_host("dns.example")
        assert set(result) == {"8.8.8.8"}
        mock_getaddrinfo.assert_called_once_with("dns.example", None, type=socket.SOCK_STREAM)


# ===========================================================================
# validate_webhook_url — the registration-time SSRF guard
# ===========================================================================


class TestValidateWebhookUrl:
    def test_urlparse_failure_reraised_as_invalid(self) -> None:
        """An unmatched IPv6 bracket makes urlparse raise → 'webhook url is invalid'.

        Exercises the pragma-no-cover except branch + kills its message mutants.
        """
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("http://[oops")
        assert str(exc.value) == "webhook url is invalid"

    def test_rejects_non_http_scheme_exact_message(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("ftp://example.com/x")
        assert str(exc.value) == "webhook url must use http or https"

    def test_accepts_http_and_https(self, mock_getaddrinfo: MagicMock) -> None:
        assert validate_webhook_url("http://example.com/x") == "http://example.com/x"
        assert validate_webhook_url("https://example.com/x") == "https://example.com/x"

    def test_rejects_missing_host_exact_message(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https:///nohost")
        assert str(exc.value) == "webhook url must include a host"

    def test_netloc_present_but_hostname_none_still_rejected(self) -> None:
        """'http://:80' has a truthy netloc but hostname is None — kills the
        ``not netloc or hostname is None`` → ``and`` mutation (which would fall
        through to ``None.rstrip`` and raise AttributeError, not ValueError)."""
        with pytest.raises(ValueError, match="webhook url must include a host"):
            validate_webhook_url("http://:80")

    def test_strips_trailing_dot_and_lowercases_hostname(self, mock_getaddrinfo: MagicMock) -> None:
        validate_webhook_url("https://EXAMPLE.COM./x")
        mock_getaddrinfo.assert_called_once_with("example.com", None, type=socket.SOCK_STREAM)

    def test_rejects_gcp_metadata_internal_name(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https://metadata.google.internal/x")
        assert str(exc.value) == "webhook url host is not allowed"

    def test_rejects_localhost_by_default(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https://localhost/x")
        assert str(exc.value) == "webhook url host is not allowed"

    def test_rejects_dot_localhost_suffix(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https://api.localhost/x")
        assert str(exc.value) == "webhook url host is not allowed"

    def test_allows_localhost_when_loopback_permitted(self) -> None:
        assert validate_webhook_url("https://localhost/x", allow_loopback_destinations=True) == "https://localhost/x"

    def test_literal_safe_ip_allowed_without_dns(self, mock_getaddrinfo: MagicMock) -> None:
        assert validate_webhook_url(f"https://{_PUBLIC_IP}/x") == f"https://{_PUBLIC_IP}/x"
        mock_getaddrinfo.assert_not_called()  # literal IP must not resolve

    def test_literal_private_ip_rejected_exact_message(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https://10.0.0.5/x")
        assert str(exc.value) == "webhook url host is not allowed"

    def test_literal_loopback_ip_allowed_only_when_flag_passed(self) -> None:
        """Threads allow_loopback into _address_allowed on the literal-IP path —
        kills the ``allow_loopback_destinations=None`` / dropped-kwarg mutants."""
        assert validate_webhook_url("https://127.0.0.1/x", allow_loopback_destinations=True) == "https://127.0.0.1/x"
        with pytest.raises(ValueError, match="host is not allowed"):
            validate_webhook_url("https://127.0.0.1/x")

    def test_dns_resolving_to_safe_ip_allowed(self, mock_getaddrinfo: MagicMock) -> None:
        mock_getaddrinfo.return_value = _addrinfo(_PUBLIC_IP)
        assert validate_webhook_url("https://safe.example/x") == "https://safe.example/x"

    def test_dns_resolving_to_loopback_allowed_only_with_flag(self, mock_getaddrinfo: MagicMock) -> None:
        """Threads allow_loopback into _address_allowed on the DNS path — kills the
        ``allow_loopback_destinations=None`` / dropped-kwarg mutants there."""
        mock_getaddrinfo.return_value = _addrinfo("127.0.0.1")
        assert validate_webhook_url("https://lo.example/x", allow_loopback_destinations=True) == "https://lo.example/x"
        with pytest.raises(ValueError, match="host is not allowed"):
            validate_webhook_url("https://lo.example/x")

    def test_dns_resolving_to_metadata_ip_rejected(self, mock_getaddrinfo: MagicMock) -> None:
        mock_getaddrinfo.return_value = _addrinfo("169.254.169.254")
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https://rebind.example/x")
        assert str(exc.value) == "webhook url host is not allowed"
        # The outer raise (line 140) re-wraps an inner raise (line 138) via `from`;
        # assert the inner cause's message too so its mutants (ValueError(None) / case
        # flips) are killable despite being swallowed by the outer handler.
        assert str(exc.value.__cause__) == "webhook url host is not allowed"

    def test_dns_failure_treated_as_not_resolved(self, mock_getaddrinfo: MagicMock) -> None:
        mock_getaddrinfo.side_effect = socket.gaierror("nxdomain")
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https://broken.example/x")
        assert str(exc.value) == "webhook url host could not be resolved"

    def test_empty_resolution_treated_as_not_resolved(self, mock_getaddrinfo: MagicMock) -> None:
        mock_getaddrinfo.return_value = []
        with pytest.raises(ValueError) as exc:
            validate_webhook_url("https://void.example/x")
        assert str(exc.value) == "webhook url host could not be resolved"


# ===========================================================================
# validate_webhook_pattern
# ===========================================================================


class TestValidateWebhookPattern:
    def test_none_passthrough(self) -> None:
        assert validate_webhook_pattern(None) is None

    def test_valid_regex_returned_unchanged(self) -> None:
        assert validate_webhook_pattern(r"\$\s") == r"\$\s"

    def test_non_string_rejected_exact_message(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_pattern(123)  # type: ignore[arg-type]
        assert str(exc.value) == "webhook pattern must be a string"

    def test_invalid_regex_rejected_exact_message(self) -> None:
        with pytest.raises(ValueError) as exc:
            validate_webhook_pattern("(unclosed")
        assert str(exc.value) == "webhook pattern is not a valid regex"


# ===========================================================================
# _address_allowed — per-IP allow/deny classification
# ===========================================================================


class TestAddressAllowed:
    @pytest.mark.parametrize("metadata_ip", ["169.254.169.254", "100.100.100.200", "fd00:ec2::254"])
    def test_metadata_ips_denied(self, metadata_ip: str) -> None:
        assert _address_allowed(metadata_ip) is False

    def test_loopback_denied_by_default(self) -> None:
        assert _address_allowed("127.0.0.1") is False

    def test_loopback_allowed_when_permitted(self) -> None:
        assert _address_allowed("127.0.0.1", allow_loopback_destinations=True) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "10.0.0.1",  # private (class A)
            "192.168.1.1",  # private (class C)
            "172.16.0.1",  # private (class B)
            "169.254.1.1",  # link-local
            "224.0.0.1",  # multicast
            "0.0.0.0",  # unspecified
            "240.0.0.1",  # reserved (class E, also private)
            "200::1",  # IPv6 reserved AND not-private — kills the `or is_reserved` → `and` mutation
        ],
    )
    def test_unsafe_ranges_denied(self, ip: str) -> None:
        assert _address_allowed(ip) is False

    @pytest.mark.parametrize("ip", [_PUBLIC_IP, "2606:2800:220:1:248:1893:25c8:1946"])
    def test_public_ips_allowed(self, ip: str) -> None:
        assert _address_allowed(ip) is True

    @pytest.mark.parametrize("ip", ["100.64.0.0", "100.64.0.1", "100.127.255.255"])
    def test_cgnat_denied(self, ip: str) -> None:
        """Kills the dropped ``ip in _CGNAT_V4`` check.

        RFC 6598 space is the one range in the refusal set that CPython does not
        classify for us — ``ipaddress.ip_address("100.64.0.1").is_private`` is
        ``False`` — so it is checked explicitly rather than inherited, and the
        explicit check is exactly what a mutant can delete without any of the
        ranges above noticing.
        """
        assert _address_allowed(ip) is False

    @pytest.mark.parametrize("ip", ["100.63.255.255", "100.128.0.0"])
    def test_cgnat_neighbours_allowed(self, ip: str) -> None:
        """Pins the /10 netmask, killing a widened prefix.

        Without these, ``100.64.0.0/10`` could be mutated to ``/8`` — swallowing
        the whole of ``100.0.0.0/8`` — and every deny case above would still
        pass. These two addresses sit immediately either side of the boundary.
        """
        assert _address_allowed(ip) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "::ffff:169.254.169.254",  # IPv4-mapped
            "2002:a9fe:a9fe::",  # 6to4 of 169.254.169.254
            "64:ff9b::169.254.169.254",  # NAT64 well-known prefix
            "::169.254.169.254",  # deprecated IPv4-compatible
        ],
    )
    def test_embedded_ipv4_metadata_forms_denied(self, ip: str) -> None:
        # Every IPv6 form that carries the v4 metadata IP must decode to it
        # before the membership check — parity with the egress connector guard.
        assert _address_allowed(ip) is False

    @pytest.mark.parametrize(
        "ip",
        [
            "::ffff:10.0.0.1",  # IPv4-mapped private
            "64:ff9b::192.168.1.1",  # NAT64 of a private v4
        ],
    )
    def test_embedded_ipv4_private_forms_denied(self, ip: str) -> None:
        assert _address_allowed(ip) is False


# ===========================================================================
# _literal_or_resolved_addresses
# ===========================================================================


class TestLiteralOrResolvedAddresses:
    def test_literal_ip_returns_single_tuple_without_resolver(self) -> None:
        resolver = MagicMock(side_effect=AssertionError("resolver must not be called for a literal IP"))
        assert _literal_or_resolved_addresses(_PUBLIC_IP, resolver) == (_PUBLIC_IP,)

    def test_hostname_delegates_to_resolver(self) -> None:
        resolver = MagicMock(return_value=("1.1.1.1",))
        assert _literal_or_resolved_addresses("host.example", resolver) == ("1.1.1.1",)
        resolver.assert_called_once_with("host.example")


# ===========================================================================
# _delivery_url_allowed — delivery-time SSRF guard (async, injected resolver)
# ===========================================================================


class TestDeliveryUrlIsLoopbackOnly:
    """`_delivery_url_is_loopback_only` is a one-line wrapper around
    `_delivery_url_allowed`, and every call site in this suite that reaches it
    through `_refuse_loopback_while_tunnel_shared` patches it away with an
    `AsyncMock` — so without a test that calls it directly and unpatched, its
    own body (the `allow_loopback_destinations=False` argument, the `not`) has
    no coverage at all and every one of its mutants reports `no tests`, which
    the mutation gate cannot pass no matter how well the *caller* is tested.
    """

    async def test_loopback_destination_is_loopback_only(self) -> None:
        assert await _delivery_url_is_loopback_only("http://127.0.0.1/x", lambda _h: (_PUBLIC_IP,)) is True

    async def test_public_destination_is_not_loopback_only(self) -> None:
        assert await _delivery_url_is_loopback_only("https://h.example", lambda _h: (_PUBLIC_IP,)) is False

    async def test_hostname_resolving_to_loopback_is_loopback_only(self) -> None:
        # Confirms the wrapper reuses the full parse/resolve/classify chain — a
        # hostname the resolver answers with 127.0.0.1, not just a literal
        # 127.0.0.1 URL — rather than a narrower private re-implementation.
        # `_delivery_url_allowed` resolves "localhost" like any other hostname
        # (the literal-string special case lives in `validate_webhook_url`, a
        # different, registration-time function), so this is what actually
        # exercises resolution rather than accidentally testing the wrong path.
        assert await _delivery_url_is_loopback_only("http://h.example/x", lambda _h: ("127.0.0.1",)) is True

    async def test_forwards_the_given_resolver_for_hostnames(self) -> None:
        calls: list[str] = []

        def resolver(host: str) -> tuple[str, ...]:
            calls.append(host)
            return (_PUBLIC_IP,)

        await _delivery_url_is_loopback_only("https://h.example", resolver)
        assert calls == ["h.example"]


class TestDeliveryUrlAllowed:
    async def test_urlparse_failure_denied(self) -> None:
        """Unmatched IPv6 bracket → urlparse raises → False (pragma-no-cover branch)."""
        assert await _delivery_url_allowed("http://[oops", lambda _h: (_PUBLIC_IP,)) is False

    async def test_rejects_non_http_scheme(self) -> None:
        assert await _delivery_url_allowed("ftp://example.com", lambda _h: (_PUBLIC_IP,)) is False

    async def test_accepts_plain_http_scheme(self) -> None:
        """An http:// (not https) URL must be allowed — kills the {"http",...} set mutants."""
        assert await _delivery_url_allowed("http://h.example", lambda _h: (_PUBLIC_IP,)) is True

    async def test_rejects_missing_host(self) -> None:
        assert await _delivery_url_allowed("https:///x", lambda _h: (_PUBLIC_IP,)) is False

    async def test_literal_safe_ip_allowed(self) -> None:
        bad = MagicMock(side_effect=AssertionError("no resolve for literal IP"))
        assert await _delivery_url_allowed(f"https://{_PUBLIC_IP}/x", bad) is True

    async def test_async_resolver_safe_allowed(self) -> None:
        async def _aresolve(_h: str) -> tuple[str, ...]:
            return (_PUBLIC_IP,)

        assert await _delivery_url_allowed("https://h.example", _aresolve) is True

    async def test_sync_resolver_unsafe_denied(self) -> None:
        assert await _delivery_url_allowed("https://h.example", lambda _h: ("10.0.0.1",)) is False

    async def test_loopback_allowed_only_with_flag(self) -> None:
        """allow_loopback threaded into _address_allowed — kills the =None / dropped mutants."""
        assert (
            await _delivery_url_allowed(
                "https://h.example", lambda _h: ("127.0.0.1",), allow_loopback_destinations=True
            )
            is True
        )
        assert await _delivery_url_allowed("https://h.example", lambda _h: ("127.0.0.1",)) is False

    async def test_resolver_raising_denied(self) -> None:
        def _boom(_h: str) -> tuple[str, ...]:
            raise OSError("dns down")

        assert await _delivery_url_allowed("https://h.example", _boom) is False

    async def test_empty_resolution_denied(self) -> None:
        assert await _delivery_url_allowed("https://h.example", lambda _h: ()) is False

    async def test_malformed_resolved_ip_denied(self) -> None:
        """A resolver returning a non-IP string makes _address_allowed raise →
        the final ValueError guard returns False (pragma-no-cover branch)."""
        assert await _delivery_url_allowed("https://h.example", lambda _h: ("not-an-ip",)) is False

    async def test_non_iterable_resolution_denied(self) -> None:
        """A resolver yielding a non-iterable value → not isinstance(..., Iterable) → False."""
        assert await _delivery_url_allowed("https://h.example", lambda _h: 42) is False  # type: ignore[arg-type,return-value]


# ===========================================================================
# WebhookManager.__init__ / delegation / registry CRUD
# ===========================================================================


class TestManagerConstruction:
    def test_default_resolver_is_resolve_host(self) -> None:
        assert WebhookManager()._resolver is _resolve_host

    def test_injected_resolver_used(self) -> None:
        r = lambda _h: (_PUBLIC_IP,)  # noqa: E731
        assert WebhookManager(resolver=r)._resolver is r

    def test_allow_loopback_coerced_to_bool(self) -> None:
        assert WebhookManager(allow_loopback_destinations=1)._allow_loopback_destinations is True
        assert WebhookManager()._allow_loopback_destinations is False

    def test_validate_url_threads_loopback_flag(self) -> None:
        assert (
            WebhookManager(allow_loopback_destinations=True).validate_url("https://localhost/x")
            == "https://localhost/x"
        )

    def test_validate_pattern_delegates(self) -> None:
        assert WebhookManager.validate_pattern(r"\$") == r"\$"
        assert WebhookManager.validate_pattern(None) is None

    def test_tunnel_tokens_stored_verbatim_not_copied(self) -> None:
        # Deliberately the live store reference, not a copy — a share created or
        # revoked after construction must still be visible to the guard. `is`
        # rather than `==` is the assertion that actually distinguishes them.
        store = {"s1": {"expires_at": 1.0}}
        assert WebhookManager(tunnel_tokens=store)._tunnel_tokens is store

    def test_tunnel_tokens_defaults_to_none(self) -> None:
        assert WebhookManager()._tunnel_tokens is None

    def test_on_metric_stored(self) -> None:
        sink = MagicMock(name="on_metric")
        assert WebhookManager(on_metric=sink)._on_metric is sink


class TestTunnelShareActive:
    """`_tunnel_share_active` is a pure function; no mocking needed."""

    def test_no_store_is_never_active(self) -> None:
        assert _tunnel_share_active(None, "s1", now=0.0) is False

    def test_unknown_session_is_never_active(self) -> None:
        assert _tunnel_share_active({}, "s1", now=0.0) is False

    def test_active_before_expiry(self) -> None:
        assert _tunnel_share_active({"s1": {"expires_at": 100.0}}, "s1", now=50.0) is True

    def test_inactive_after_expiry(self) -> None:
        assert _tunnel_share_active({"s1": {"expires_at": 100.0}}, "s1", now=150.0) is False

    def test_exact_expiry_instant_is_not_active(self) -> None:
        # Pinned by conformance/EGRESS_GUARD.md §4: `now == expires_at` counts as
        # expired here, the opposite of sweep_expired_tunnel_tokens' `now >
        # expires_at`. A `<` weakened to `<=` would flip only this one instant.
        assert _tunnel_share_active({"s1": {"expires_at": 100.0}}, "s1", now=100.0) is False

    def test_missing_expiry_fails_closed(self) -> None:
        assert _tunnel_share_active({"s1": {}}, "s1", now=0.0) is True

    def test_non_numeric_expiry_fails_closed(self) -> None:
        assert _tunnel_share_active({"s1": {"expires_at": "soon"}}, "s1", now=0.0) is True

    def test_looks_up_the_given_session_only(self) -> None:
        store = {"s1": {"expires_at": 100.0}, "s2": {"expires_at": 0.0}}
        assert _tunnel_share_active(store, "s1", now=50.0) is True
        assert _tunnel_share_active(store, "s2", now=50.0) is False


class TestRegistryCrud:
    async def test_register_validates_url_and_stores_fields(self) -> None:
        mgr = _make_manager()
        cfg = await mgr.register("s1", _URL, event_types=["snapshot"], pattern=r"\$", secret="k", event_bus=None)
        assert isinstance(cfg, WebhookConfig)
        assert cfg.url == _URL  # register assigns the validated url (kills url=None)
        assert cfg.session_id == "s1"
        assert cfg.event_types == frozenset({"snapshot"})
        assert cfg.pattern == r"\$"
        assert cfg.secret == "k"
        assert cfg.webhook_id  # uuid assigned (kills webhook_id=None)
        await mgr.shutdown()

    async def test_register_rejects_unsafe_url(self) -> None:
        """register must call validate_url (kills url=None, which would skip validation)."""
        mgr = _make_manager()
        with pytest.raises(ValueError, match="host is not allowed"):
            await mgr.register("s1", "https://10.0.0.1/x", event_bus=None)

    async def test_register_then_get_and_list(self) -> None:
        mgr = _make_manager()
        cfg = await mgr.register("s1", _URL, event_bus=None)
        assert mgr.get_webhook(cfg.webhook_id) is cfg
        assert mgr.list_webhooks("s1") == [cfg]
        assert mgr.list_webhooks("other") == []
        await mgr.shutdown()

    async def test_register_event_types_none_stays_none(self) -> None:
        mgr = _make_manager()
        cfg = await mgr.register("s1", _URL, event_bus=None)
        assert cfg.event_types is None
        await mgr.shutdown()

    async def test_register_done_callback_logs_on_task_exception(self) -> None:
        """A delivery task that raises must trigger the done-callback error log with
        EXACT args — kills the lambda-replacement, format, arg, and condition mutants."""
        mgr = _make_manager()
        bus = MagicMock()
        with (
            patch.object(mgr, "_delivery_loop", AsyncMock(side_effect=RuntimeError("loop boom"))),
            patch("provide.uterm.server.webhooks.logger") as log,
        ):
            cfg = await mgr.register("s1", _URL, event_bus=bus)
            # the delivery loop is started with the exact (cfg, event_bus) — kills the
            # create_task(self._delivery_loop(None, ...)) / (..., None) arg mutants.
            mgr._delivery_loop.assert_called_once_with(cfg, bus)
            with pytest.raises(RuntimeError):
                await mgr._tasks[cfg.webhook_id]
        log.error.assert_called_once()
        args = log.error.call_args.args
        assert args[0] == "webhook_delivery_loop_failed webhook_id=%s error=%s"
        assert args[1] == cfg.webhook_id
        assert isinstance(args[2], RuntimeError)

    async def test_register_done_callback_silent_on_success(self) -> None:
        """A task that completes cleanly (event_bus=None) must NOT log — kills the
        condition mutants (`and`→`or`, `is not None`→`is None`)."""
        mgr = _make_manager()
        with patch("provide.uterm.server.webhooks.logger") as log:
            cfg = await mgr.register("s1", _URL, event_bus=None)
            await asyncio.wait_for(mgr._tasks[cfg.webhook_id], timeout=1.0)
            await asyncio.sleep(0)
        log.error.assert_not_called()

    async def test_unregister_found_true_unknown_false(self) -> None:
        mgr = _make_manager()
        cfg = await mgr.register("s1", _URL, event_bus=None)
        assert await mgr.unregister(cfg.webhook_id) is True
        assert await mgr.unregister(cfg.webhook_id) is False
        assert await mgr.unregister("never") is False
        await mgr.shutdown()

    async def test_unregister_orphan_webhook_without_task_returns_true(self) -> None:
        """A webhook present in _webhooks but absent from _tasks must unregister
        cleanly. Kills ``_tasks.pop(id, None)`` → ``_tasks.pop(id)`` (KeyError) and
        ``task is not None and ...`` → ``or`` (None.done() AttributeError)."""
        mgr = _make_manager()
        mgr._webhooks["orphan"] = _cfg(webhook_id="orphan")
        # deliberately NOT in mgr._tasks
        assert await mgr.unregister("orphan") is True

    async def test_unregister_cancels_live_task(self) -> None:
        """A live (not-done) delivery task must be cancelled by unregister — kills the
        `if task is not None and not task.done()` branch mutants."""
        mgr = _make_manager()
        started = asyncio.Event()

        async def _block(_cfg: WebhookConfig, _bus: Any) -> None:
            started.set()
            await asyncio.Event().wait()  # never completes until cancelled

        with patch.object(mgr, "_delivery_loop", _block):
            cfg = await mgr.register("s1", _URL, event_bus=MagicMock())
            await asyncio.wait_for(started.wait(), timeout=1.0)
            task = mgr._tasks[cfg.webhook_id]
            # wait_for guard: a mutant that breaks the cancel path would make the
            # internal `await task` hang forever — bound it so it fails fast (TimeoutError
            # = a kill) instead of stalling the whole mutmut worker past the 30s timeout.
            assert await asyncio.wait_for(mgr.unregister(cfg.webhook_id), timeout=5.0) is True
            assert task.cancelled()

    async def test_unregister_clears_blocked_count(self) -> None:
        mgr = _make_manager()
        cfg = await mgr.register("s1", _URL, event_bus=None)
        mgr._blocked_counts[cfg.webhook_id] = 2
        await mgr.unregister(cfg.webhook_id)
        assert cfg.webhook_id not in mgr._blocked_counts

    async def test_shutdown_cancels_live_tasks_and_clears(self) -> None:
        mgr = _make_manager()
        started = asyncio.Event()

        async def _brief(_cfg: WebhookConfig, _bus: Any) -> None:
            # Completes on its own shortly. If shutdown's ``if not task.done()`` guard
            # is mutated to ``if task.done()`` it WON'T cancel this still-running task,
            # so the task finishes normally (not cancelled) — a fast assertion failure
            # rather than a gather()-hang/timeout.
            started.set()
            await asyncio.sleep(0.05)

        with patch.object(mgr, "_delivery_loop", _brief):
            cfg = await mgr.register("s1", _URL, event_bus=MagicMock())
            await asyncio.wait_for(started.wait(), timeout=1.0)
            task = mgr._tasks[cfg.webhook_id]
            # NB: NOT wrapped in wait_for. The _brief task self-completes in 0.05s so no
            # mutant can hang shutdown's gather; and crucially, wait_for would add an
            # event-loop yield that lets a cancel-requested task settle — which would mask
            # `gather(*tasks)` → `gather()` (mutmut_5: drops the await, leaving the task
            # cancel-requested-but-not-awaited). Asserting cancelled() with no intervening
            # yield keeps that mutant killable.
            await mgr.shutdown()
        assert task.cancelled()
        assert mgr.list_webhooks("s1") == []
        assert mgr._tasks == {}
        assert mgr._blocked_counts == {}


# ===========================================================================
# WebhookManager._delivery_loop
# ===========================================================================


class TestDeliveryLoop:
    async def test_no_event_bus_returns_immediately(self) -> None:
        mgr = _make_manager()
        await mgr._delivery_loop(_cfg(), None)  # returns without touching watch/_deliver

    async def test_watch_called_with_session_event_types_and_pattern(self) -> None:
        mgr = _make_manager()
        delivered: list[tuple[WebhookConfig, dict[str, Any]]] = []

        async def _record(cfg_arg: WebhookConfig, event: dict[str, Any]) -> None:
            delivered.append((cfg_arg, event))

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        ev = _make_event()
        await queue.put(ev)
        await queue.put(None)  # sentinel → loop returns
        sub = MagicMock()
        sub.queue = queue
        watch_cm = MagicMock()
        watch_cm.__aenter__ = AsyncMock(return_value=sub)
        watch_cm.__aexit__ = AsyncMock(return_value=False)
        bus = MagicMock()
        bus.watch = MagicMock(return_value=watch_cm)

        cfg = WebhookConfig(
            webhook_id="wh1",
            session_id="sess-X",
            url=_URL,
            event_types=frozenset({"snapshot"}),
            pattern=r"\$",
            secret=None,
        )
        with patch.object(mgr, "_deliver", _record):
            # wait_for guard: the `if item is None: return` sentinel mutated to
            # `is not None` makes the loop consume the sentinel, deliver, then block on
            # an empty queue.get() forever. Bound it so that hang fails fast (a kill)
            # rather than stalling the mutmut worker past its 30s timeout.
            await asyncio.wait_for(mgr._delivery_loop(cfg, bus), timeout=5.0)
        # delivered the queued event AND passed the real cfg (kills _deliver(None, item))
        assert delivered == [(cfg, ev)]
        bus.watch.assert_called_once_with("sess-X", event_types=["snapshot"], pattern=r"\$")

    async def test_event_types_none_passes_none_to_watch(self) -> None:
        mgr = _make_manager()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        await queue.put(None)
        sub = MagicMock()
        sub.queue = queue
        watch_cm = MagicMock()
        watch_cm.__aenter__ = AsyncMock(return_value=sub)
        watch_cm.__aexit__ = AsyncMock(return_value=False)
        bus = MagicMock()
        bus.watch = MagicMock(return_value=watch_cm)
        # bounded: the `is None`→`is not None` sentinel mutant would hang here on the
        # now-empty queue; wait_for turns that into a fast TimeoutError kill.
        await asyncio.wait_for(mgr._delivery_loop(_cfg(), bus), timeout=5.0)  # cfg.event_types is None
        bus.watch.assert_called_once_with("s1", event_types=None, pattern=None)


# ===========================================================================
# WebhookManager._deliver — SSRF block / auto-unregister / retry / signing
# ===========================================================================


class _DeliverEnv:
    """Bundle of every network/observability patch _deliver touches."""

    def __init__(self) -> None:
        self.guard = AsyncMock(name="_delivery_url_allowed", return_value=True)
        self.post = AsyncMock(name="post", return_value=_resp(success=True))
        self.sleep = AsyncMock(name="sleep")
        self.log = MagicMock(name="logger")
        self.inject = MagicMock(name="inject_trace_context")
        self.client_cls = MagicMock(name="AsyncClient")
        client = MagicMock(name="client")
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = self.post
        self.client_cls.return_value = client


@pytest.fixture
def denv() -> Any:
    env = _DeliverEnv()
    with (
        patch("provide.uterm.server.webhooks._delivery_url_allowed", env.guard),
        patch("provide.uterm.server.webhooks.asyncio.sleep", env.sleep),
        patch("provide.uterm.server.webhooks.logger", env.log),
        patch("provide.uterm.server.webhooks.inject_trace_context", env.inject),
        patch("httpx2.AsyncClient", env.client_cls),
    ):
        yield env


class TestRefuseLoopbackWhileTunnelShared:
    """`_refuse_loopback_while_tunnel_shared` — the two collaborators it calls
    (`_tunnel_share_active`, `_delivery_url_is_loopback_only`) are patched
    directly rather than driven through real tunnel-token/DNS state, so every
    branch of the guard itself is reachable and observable independent of them.
    """

    @staticmethod
    @contextlib.contextmanager
    def _patches(*, share_active: bool, loopback_only: bool):
        # Both collaborators are yielded as their own mocks (not just entered
        # and discarded): the first round of this suite patched them with plain
        # `return_value=...`/`AsyncMock(return_value=...)` and asserted only the
        # boolean outcome, so a mutant that scrambled *which* argument each
        # collaborator was called with — `self._tunnel_tokens` swapped for
        # `None`, `cfg.session_id` dropped, `cfg.url`/`self._resolver` swapped —
        # was invisible to every test here and survived. Returning the mocks
        # lets each test assert `assert_called_once_with(...)` on the exact
        # arguments, closing that gap.
        with (
            patch("provide.uterm.server.webhooks._tunnel_share_active", return_value=share_active) as share_mock,
            patch(
                "provide.uterm.server.webhooks._delivery_url_is_loopback_only",
                AsyncMock(return_value=loopback_only),
            ) as loopback_mock,
            patch("provide.uterm.server.webhooks.logger") as log,
        ):
            yield share_mock, loopback_mock, log

    async def test_no_share_is_never_refused(self) -> None:
        metric = MagicMock(name="on_metric")
        mgr = _make_manager(tunnel_tokens={}, on_metric=metric)
        cfg = _cfg()
        with self._patches(share_active=False, loopback_only=True) as (share_mock, loopback_mock, _log):
            assert await mgr._refuse_loopback_while_tunnel_shared(cfg) is False
        share_mock.assert_called_once_with(mgr._tunnel_tokens, cfg.session_id, now=pytest.approx(time.time(), abs=5))
        loopback_mock.assert_not_awaited()
        metric.assert_not_called()

    async def test_share_active_but_destination_not_loopback_only_is_not_refused(self) -> None:
        metric = MagicMock(name="on_metric")
        mgr = _make_manager(tunnel_tokens={}, on_metric=metric)
        cfg = _cfg()
        with self._patches(share_active=True, loopback_only=False) as (_share_mock, loopback_mock, _log):
            assert await mgr._refuse_loopback_while_tunnel_shared(cfg) is False
        loopback_mock.assert_awaited_once_with(cfg.url, mgr._resolver)
        metric.assert_not_called()

    async def test_share_active_and_loopback_only_is_refused_and_counted(self) -> None:
        metric = MagicMock(name="on_metric")
        mgr = _make_manager(tunnel_tokens={}, on_metric=metric)
        with self._patches(share_active=True, loopback_only=True):
            assert await mgr._refuse_loopback_while_tunnel_shared(_cfg()) is True
        # The dedicated counter, not the generic one that feeds auto-unregister —
        # a share can be revoked, so this refusal must not retire the webhook.
        metric.assert_called_once_with("webhook_delivery_blocked_tunnel_total", 1)

    async def test_refusal_logs_the_exact_share_reason_with_webhook_and_session_ids(self) -> None:
        mgr = _make_manager(tunnel_tokens={})
        cfg = _cfg(webhook_id="wh-x", session_id="sess-y")
        with self._patches(share_active=True, loopback_only=True) as (_share_mock, _loopback_mock, log):
            await mgr._refuse_loopback_while_tunnel_shared(cfg)
        # The exact message, not a substring: an earlier version of this test
        # checked only `"...tunnel_shared" in args.args[0]`, which a mutant that
        # corrupted the *other* half of the (two-literal, implicitly
        # concatenated) message satisfied without being caught.
        log.warning.assert_called_once_with(
            "webhook_delivery_blocked webhook_id=%s url=%s session_id=%s "
            "reason=loopback_destination_while_tunnel_shared",
            cfg.webhook_id,
            cfg.url,
            cfg.session_id,
        )

    async def test_no_metric_sink_does_not_raise(self) -> None:
        mgr = _make_manager(tunnel_tokens={})
        with self._patches(share_active=True, loopback_only=True):
            assert await mgr._refuse_loopback_while_tunnel_shared(_cfg()) is True

    async def test_refusal_does_not_touch_blocked_counts(self) -> None:
        # A share retiring itself must not feed the SSRF-guard kill switch: only
        # `_deliver`'s destination-safety branch may increment `_blocked_counts`.
        mgr = _make_manager(tunnel_tokens={})
        cfg = _cfg()
        mgr._blocked_counts[cfg.webhook_id] = 2
        with self._patches(share_active=True, loopback_only=True):
            await mgr._refuse_loopback_while_tunnel_shared(cfg)
        assert mgr._blocked_counts[cfg.webhook_id] == 2


class TestDeliver:
    async def test_guard_called_with_exact_args(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        await mgr._deliver(_cfg(), _make_event())
        denv.guard.assert_awaited_once_with(_URL, mgr._resolver, allow_loopback_destinations=False)

    async def test_success_path_posts_once_with_json_body_and_headers(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        await mgr._deliver(_cfg(), _make_event())
        assert denv.post.await_count == 1
        assert denv.post.await_args.args[0] == _URL
        kwargs = denv.post.await_args.kwargs
        body = json.loads(kwargs["content"])
        assert body["webhook_id"] == "wh1"
        assert body["session_id"] == "s1"
        assert body["event"]["type"] == "snapshot"
        assert "timestamp" in body
        assert kwargs["headers"]["Content-Type"] == "application/json"

    async def test_client_constructed_with_deliver_timeout(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        await mgr._deliver(_cfg(), _make_event())
        denv.client_cls.assert_called_once_with(timeout=_DELIVER_TIMEOUT_S)

    async def test_trace_context_injected_into_headers(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        await mgr._deliver(_cfg(), _make_event())
        denv.inject.assert_called_once()
        assert isinstance(denv.inject.call_args.args[0], dict)  # the headers dict, not None

    async def test_signed_delivery_adds_numeric_timestamp_and_signature(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        await mgr._deliver(_cfg(secret="k"), _make_event())
        headers = denv.post.await_args.kwargs["headers"]
        assert headers["X-Uterm-Signature"]
        float(headers["X-Uterm-Timestamp"])  # str(time.time()), not str(None) → must parse

    async def test_unsigned_delivery_omits_signature_headers(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        await mgr._deliver(_cfg(secret=None), _make_event())
        assert "X-Uterm-Signature" not in denv.post.await_args.kwargs["headers"]

    async def test_guard_block_increments_count_emits_metric_logs_no_post(self, denv: _DeliverEnv) -> None:
        denv.guard.return_value = False
        metric = MagicMock()
        mgr = _make_manager(on_metric=metric)
        await mgr._deliver(_cfg(), _make_event())
        assert denv.post.await_count == 0
        assert mgr._blocked_counts["wh1"] == 1
        metric.assert_any_call("webhook_delivery_blocked_total", 1)
        denv.log.warning.assert_called_once_with(
            "webhook_delivery_blocked webhook_id=%s url=%s reason=unsafe_destination count=%d", "wh1", _URL, 1
        )

    async def test_guard_pass_resets_block_count(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        mgr._blocked_counts["wh1"] = 2
        await mgr._deliver(_cfg(), _make_event())
        assert "wh1" not in mgr._blocked_counts

    async def test_block_threshold_triggers_auto_unregister(self) -> None:
        # NB: deliberately does NOT use the `denv` fixture — the block path makes no
        # httpx2 call and no _deliver-internal sleep, and we need the REAL asyncio.sleep
        # to flush the unregister task's done-callback below.
        metric = MagicMock()
        mgr = _make_manager(on_metric=metric)
        mgr._blocked_counts["wh1"] = _MAX_BLOCKED_DELIVERIES - 1
        with (
            patch("provide.uterm.server.webhooks._delivery_url_allowed", AsyncMock(return_value=False)),
            patch("provide.uterm.server.webhooks.logger") as log,
            patch.object(mgr, "unregister", AsyncMock(return_value=True)) as unreg,
        ):
            await mgr._deliver(_cfg(), _make_event())
            # the unregister task is tracked with a strong ref (kills add(None))
            assert None not in mgr._unregister_tasks
            tracked = list(mgr._unregister_tasks)
            assert len(tracked) == 1
            await asyncio.gather(*tracked)  # run the scheduled unregister
            await asyncio.sleep(0)  # flush its done-callback (the discard)
        metric.assert_any_call("webhook_auto_unregistered_total", 1)
        unreg.assert_awaited_once_with("wh1")
        log.error.assert_called_once_with(
            "webhook_auto_unregistered webhook_id=%s url=%s reason=ssrf_guard_threshold count=%d",
            "wh1",
            _URL,
            _MAX_BLOCKED_DELIVERIES,
        )
        # original discards the completed unregister task via add_done_callback(discard);
        # mutmut_58 (add_done_callback(None)) leaves it stuck in the strong-ref set.
        assert mgr._unregister_tasks == set()

    async def test_block_below_threshold_does_not_unregister(self, denv: _DeliverEnv) -> None:
        denv.guard.return_value = False
        mgr = _make_manager()
        with patch.object(mgr, "unregister", AsyncMock()) as unreg:
            await mgr._deliver(_cfg(), _make_event())  # count 1, below 3
            await asyncio.sleep(0)
        unreg.assert_not_called()

    async def test_first_attempt_success_skips_retries(self, denv: _DeliverEnv) -> None:
        mgr = _make_manager()
        await mgr._deliver(_cfg(), _make_event())
        assert denv.post.await_count == 1
        assert denv.sleep.await_count == 0

    async def test_retries_then_gives_up_on_failure_status(self, denv: _DeliverEnv) -> None:
        denv.post.return_value = _resp(success=False, status=500)
        metric = MagicMock()
        mgr = _make_manager(on_metric=metric)
        await mgr._deliver(_cfg(), _make_event())
        assert denv.post.await_count == 4  # 3 retries + final attempt
        # inter-attempt sleeps use the real retry delays in order (kills sleep(None))
        assert [c.args[0] for c in denv.sleep.await_args_list] == [0.5, 1.0, 2.0]
        metric.assert_any_call("webhook_delivery_failed_total", 1)
        metric.assert_any_call("webhook_delivery_giving_up_total", 1)
        # FIRST failed-delivery log: exact format + 1-based attempt. Assert the first
        # call specifically (not assert_any_call) so `attempt + 1` → `attempt - 1`
        # is killed — across attempts 0..3 the -1 variant yields -1,0,1,2 which would
        # spuriously satisfy a loose "any call has attempt=1".
        assert denv.log.warning.call_args_list[0].args == (
            "webhook_delivery_failed webhook_id=%s url=%s status=%d attempt=%d",
            "wh1",
            _URL,
            500,
            1,
        )
        denv.log.error.assert_called_once_with("webhook_delivery_giving_up webhook_id=%s url=%s", "wh1", _URL)

    async def test_exception_path_retries_and_logs_error(self, denv: _DeliverEnv) -> None:
        denv.post.side_effect = RuntimeError("connect refused")
        metric = MagicMock()
        mgr = _make_manager(on_metric=metric)
        await mgr._deliver(_cfg(), _make_event())
        assert denv.post.await_count == 4
        assert denv.sleep.await_count == 3
        metric.assert_any_call("webhook_delivery_giving_up_total", 1)
        # first error log: exact format + the exception object + 1-based attempt
        first = denv.log.warning.call_args_list[0]
        assert first.args[0] == "webhook_delivery_error webhook_id=%s url=%s error=%s attempt=%d"
        assert first.args[1] == "wh1"
        assert first.args[2] == _URL
        assert isinstance(first.args[3], RuntimeError)
        assert first.args[4] == 1
