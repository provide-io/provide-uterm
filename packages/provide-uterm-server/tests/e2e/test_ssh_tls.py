#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Gap #5: prove the SSH → identity-frame → WS pipeline over wss:// (TLS).

Production uses ``wss://``; every other e2e test uses plain ``ws://``.  This
file covers the missing path: a self-signed TLS certificate is generated with
:mod:`cryptography`, a ``websockets.serve`` server is started with that cert,
and an :class:`SshWsGateway` is pointed at the resulting ``wss://`` URL.

Two outcomes are possible and both carry useful information:

* **PASS** — ``websockets.connect`` in ``_ssh_handler.py`` happens to trust the
  cert (e.g. via ``SSL_CERT_FILE`` env var or the OS trust store), meaning the
  ``wss://`` path already works end-to-end.
* **FAIL** — the first message never arrives because ``websockets.connect``
  raises ``ssl.SSLCertVerificationError`` for the self-signed cert, confirming
  gap #5: the gateway has no mechanism to inject an ``ssl=`` context for
  self-signed endpoints.  The test is marked ``xfail`` with this reason so the
  CI result is informative rather than blocking.

Either way the test is NOT silenced — the assertion inside ``xfail(strict=False)``
runs, and a genuine pass promotes the test to a "pass" in the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import ipaddress
import ssl
from pathlib import Path
from typing import Any

import asyncssh
import pytest
import websockets
import websockets.server
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from provide.terminal.auth import AuthorizedKeysFileResolver
from provide.terminal.gateway import SshWsGateway
from tests.bridge.control_channel_helpers import decode_control_payload

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Certificate helpers
# ---------------------------------------------------------------------------


def _generate_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    """Generate a self-signed RSA certificate for 127.0.0.1.

    Returns ``(cert_path, key_path)`` as PEM files written under
    ``tmp_path``.  The certificate is valid for ``localhost`` / ``127.0.0.1``
    so that Python's hostname-verification passes for the loopback address.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    return cert_path, key_path


def _server_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Return an SSL context for use by the *server* (websockets.serve)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def _client_ssl_context(cert_path: Path) -> ssl.SSLContext:
    """Return an SSL context that trusts *only* our self-signed cert.

    Used only in the helper that verifies the WS server itself; the
    gateway's own websockets.connect call does NOT receive this context —
    that is precisely what gap #5 is documenting.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(str(cert_path))
    ctx.check_hostname = True
    return ctx


# ---------------------------------------------------------------------------
# WS server helper
# ---------------------------------------------------------------------------


async def _start_tls_ws_server(cert_path: Path, key_path: Path) -> tuple[Any, int, list[dict[str, Any]]]:
    """Start a ``wss://`` server that captures the first control frame.

    Returns ``(server, port, captured_frames)``.
    """
    captured: list[dict[str, Any]] = []

    async def _handler(ws: Any) -> None:
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    with contextlib.suppress(Exception):
                        captured.append(decode_control_payload(msg))
        except websockets.ConnectionClosed:
            pass

    server_ctx = _server_ssl_context(cert_path, key_path)
    srv = await websockets.serve(_handler, "127.0.0.1", 0, ssl=server_ctx)
    port: int = srv.sockets[0].getsockname()[1]
    return srv, port, captured


# ---------------------------------------------------------------------------
# Authorized-keys fixture helper
# ---------------------------------------------------------------------------


async def _write_authorized_keys(tmp_path: Path, pubkey: asyncssh.SSHKey, subject: str) -> Path:
    line = pubkey.export_public_key().decode("ascii").strip()
    opts = f'subject="{subject}",claim-role="operator"'
    path = tmp_path / "authorized_keys"
    path.write_text(f"{opts} {line}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSshTlsPipeline:
    """Gap #5: SSH → identity-frame → wss:// WebSocket pipeline."""

    async def test_identity_frame_arrives_over_wss(self, tmp_path: Path) -> None:
        """SSH client with registered pubkey → gateway → wss:// → identity frame received.

        Gap #5 (now closed): ``SshWsGateway(..., ws_ssl=<context>)`` forwards
        the SSL context to the upstream ``websockets.connect`` so
        self-signed / internal-CA deployments work. This test constructs a
        trust context that includes the test cert, hands it to the gateway,
        and confirms the full pipeline lands an identity frame.
        """
        import ssl

        # --- generate a self-signed TLS cert for the WS server -----------
        cert_path, key_path = _generate_self_signed_cert(tmp_path)

        # --- SSH keypair + authorized_keys --------------------------------
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        authorized_keys = await _write_authorized_keys(tmp_path, client_key, subject="sre:gap5")
        resolver = AuthorizedKeysFileResolver(authorized_keys)

        # --- wss:// WS server (self-signed cert) --------------------------
        ws_srv, ws_port, captured = await _start_tls_ws_server(cert_path, key_path)
        wss_url = f"wss://127.0.0.1:{ws_port}/"

        # --- Build a client SSL context that trusts the test cert --------
        client_ctx = ssl.create_default_context()
        client_ctx.load_verify_locations(str(cert_path))
        # 127.0.0.1 cert is self-signed; don't require hostname check.
        client_ctx.check_hostname = False

        # --- SshWsGateway pointing at wss:// WITH the trust context -----
        gw = SshWsGateway(wss_url, key_resolver=resolver, ws_ssl=client_ctx)
        ssh_srv = await gw.start("127.0.0.1", 0)
        ssh_port: int = ssh_srv.sockets[0].getsockname()[1]

        try:
            async with (
                asyncssh.connect(
                    "127.0.0.1",
                    port=ssh_port,
                    known_hosts=None,
                    username="gap5",
                    client_keys=[client_key],
                    preferred_auth="publickey",
                    agent_path=None,
                    config=[],
                ) as conn,
                conn.create_process() as proc,
            ):
                proc.stdin.write_eof()
                # Give the gateway time to attempt the WS connection and
                # either succeed (frame arrives) or fail (TLS error logged).
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.stdout.read(4096), timeout=3.0)
                await asyncio.sleep(0.3)
        finally:
            ssh_srv.close()
            await ssh_srv.wait_closed()
            ws_srv.close()

        # With ws_ssl= wired in, the gateway can handshake against the
        # self-signed endpoint and the identity frame lands on the WS.
        assert len(captured) >= 1, (
            "No control frame reached the wss:// server — the ws_ssl trust "
            "context did not take effect inside _make_process_handler."
        )
        first = captured[0]
        assert first.get("type") == "identity", f"unexpected first frame: {first!r}"
        assert first.get("subject") == "sre:gap5"

    async def test_wss_server_itself_is_reachable_with_trusted_cert(self, tmp_path: Path) -> None:
        """Sanity: the wss:// test server works when the client trusts its cert.

        This test does NOT go through the gateway — it connects directly with a
        client ssl context that trusts the self-signed cert.  If this passes but
        ``test_identity_frame_arrives_over_wss`` xfails, the problem is
        conclusively in the gateway (not in the test server setup).
        """
        cert_path, key_path = _generate_self_signed_cert(tmp_path)

        received: list[str] = []

        async def _echo(ws: Any) -> None:
            async for msg in ws:
                received.append(msg)
                await ws.send(msg)

        server_ctx = _server_ssl_context(cert_path, key_path)
        srv = await websockets.serve(_echo, "127.0.0.1", 0, ssl=server_ctx)
        port: int = srv.sockets[0].getsockname()[1]

        try:
            client_ctx = _client_ssl_context(cert_path)
            async with websockets.connect(f"wss://127.0.0.1:{port}/", ssl=client_ctx) as ws:
                await ws.send("hello-tls")
                reply = await asyncio.wait_for(ws.recv(), timeout=3.0)
        finally:
            srv.close()

        assert reply == "hello-tls", f"echo server returned {reply!r}"
