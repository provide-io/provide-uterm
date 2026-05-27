#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for SSHStreamReader and SSHStreamWriter (mock asyncssh process)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("asyncssh", reason="asyncssh not installed; skip SSH transport tests")

from provide.uterm.transports.ssh import SSHStreamWriter


class MockStdin:
    def __init__(self, data: bytes | str) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes | str:
        return self._data


class MockStdout:
    def __init__(self) -> None:
        self.written: bytearray = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass


from typing import cast

import asyncssh


class MockProcess:
    def __init__(self, stdin_data: bytes | str = b"") -> None:
        self.stdin = MockStdin(stdin_data)
        self.stdout = MockStdout()
        self._exited = False
        self._closed = False

    def exit(self, code: int) -> None:
        self._exited = True

    def close(self) -> None:
        self._closed = True

    def get_extra_info(self, name: str) -> object:
        if name == "peername":
            return ("127.0.0.1", 12345)
        return None


class TestSSHStreamWriterDoubleClose:
    def test_close_when_already_closed_is_noop(self) -> None:
        """Line 87->exit: _closed is already True → skip close body."""
        proc = MockProcess()
        writer = SSHStreamWriter(cast("asyncssh.SSHServerProcess[bytes]", proc))
        writer._closed = True
        writer.close()  # Should be a no-op, not call exit/close again
        assert writer._closed


# ---------------------------------------------------------------------------
# Finding #6 — credential validation wiring
# ---------------------------------------------------------------------------


class TestCredentialValidators:
    """TerminalSSHServer must consult validators when provided, and start_ssh_server
    must refuse a permissive bind on a non-loopback address.
    """

    def test_password_validator_accepts(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        calls: list[tuple[str, str]] = []

        def _validator(user: str, pw: str) -> bool:
            calls.append((user, pw))
            return user == "alice" and pw == "secret"

        server = TerminalSSHServer({}, max_connections_per_ip=5, password_validator=_validator)
        assert server.validate_password("alice", "secret") is True
        assert server.validate_password("alice", "wrong") is False
        assert calls == [("alice", "secret"), ("alice", "wrong")]

    def test_public_key_validator_consulted(self) -> None:
        from provide.uterm.transports.ssh import TerminalSSHServer

        seen: list[str] = []

        def _validator(user: str, key) -> bool:
            seen.append(user)
            return user == "ok-user"

        server = TerminalSSHServer({}, max_connections_per_ip=5, public_key_validator=_validator)
        assert server.validate_public_key("ok-user", MagicMock()) is True
        assert server.validate_public_key("denied", MagicMock()) is False
        assert seen == ["ok-user", "denied"]

    def test_none_validators_preserve_backcompat(self) -> None:
        """When no validators are supplied the server stays permissive (gateway case)."""
        from provide.uterm.transports.ssh import TerminalSSHServer

        server = TerminalSSHServer({}, max_connections_per_ip=5)
        assert server.validate_password("anyone", "anything") is True
        assert server.validate_public_key("anyone", MagicMock()) is True

    async def test_none_validators_non_loopback_raises(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import start_ssh_server

        async def _handler(reader, writer) -> None:  # pragma: no cover - never invoked
            return None

        with pytest.raises(RuntimeError, match="non-loopback"):
            await start_ssh_server(
                _handler,
                host="0.0.0.0",
                port=0,
                host_key_path=tmp_path,
            )

    async def test_none_validators_loopback_allowed(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import start_ssh_server

        async def _handler(reader, writer) -> None:  # pragma: no cover - never invoked
            return None

        async def _create_server(server_class, host, port, **kwargs):
            return MagicMock()

        with patch("provide.uterm.transports.ssh.asyncssh.create_server", side_effect=_create_server):
            srv = await start_ssh_server(_handler, host="127.0.0.1", port=0, host_key_path=tmp_path)
        assert srv is not None

    async def test_custom_validator_non_loopback_allowed(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import start_ssh_server

        async def _handler(reader, writer) -> None:  # pragma: no cover - never invoked
            return None

        async def _create_server(server_class, host, port, **kwargs):
            # Confirm the factory bakes our validator in.
            instance = server_class()
            assert instance.validate_password("u", "good") is True
            assert instance.validate_password("u", "bad") is False
            return MagicMock()

        with patch("provide.uterm.transports.ssh.asyncssh.create_server", side_effect=_create_server):
            srv = await start_ssh_server(
                _handler,
                host="0.0.0.0",
                port=0,
                host_key_path=tmp_path,
                credentials_validator=lambda _u, p: p == "good",
            )
        assert srv is not None

    async def test_allow_unauthenticated_non_loopback(self, tmp_path) -> None:
        from provide.uterm.transports.ssh import start_ssh_server

        async def _handler(reader, writer) -> None:  # pragma: no cover - never invoked
            return None

        async def _create_server(server_class, host, port, **kwargs):
            return MagicMock()

        with patch("provide.uterm.transports.ssh.asyncssh.create_server", side_effect=_create_server):
            srv = await start_ssh_server(
                _handler,
                host="0.0.0.0",
                port=0,
                host_key_path=tmp_path,
                allow_unauthenticated=True,
            )
        assert srv is not None


# ---------------------------------------------------------------------------
# Finding #20 — host key file permission / ownership checks
# ---------------------------------------------------------------------------


class TestHostKeyPermissions:
    def test_rejects_world_readable_key(self, tmp_path) -> None:
        import asyncssh

        from provide.uterm.transports.ssh import _get_or_create_host_key

        existing = asyncssh.generate_private_key("ssh-ed25519")
        key_path = tmp_path / "ssh_host_key"
        key_path.write_bytes(existing.export_private_key())
        key_path.chmod(0o644)  # too permissive

        with pytest.raises(PermissionError, match="insecure mode"):
            _get_or_create_host_key(tmp_path)

    def test_rejects_foreign_owned_key(self, tmp_path) -> None:
        import asyncssh

        from provide.uterm.transports.ssh import _get_or_create_host_key

        existing = asyncssh.generate_private_key("ssh-ed25519")
        key_path = tmp_path / "ssh_host_key"
        key_path.write_bytes(existing.export_private_key())
        key_path.chmod(0o600)

        real_stat = key_path.stat()

        # Fake stat result where st_uid differs from current uid.
        class _FakeStat:
            st_mode = real_stat.st_mode
            st_uid = real_stat.st_uid + 1
            st_gid = real_stat.st_gid

        with patch("provide.uterm.transports.ssh.os.stat", return_value=_FakeStat):
            with pytest.raises(PermissionError, match="owned by uid"):
                _get_or_create_host_key(tmp_path)

    def test_default_host_key_dir_uses_home(self) -> None:
        from pathlib import Path

        from provide.uterm.transports.ssh import _default_host_key_dir

        fake_home = Path("/tmp/uterm-fake-home-1234567")
        with patch("provide.uterm.transports.ssh.Path.home", return_value=fake_home):
            assert _default_host_key_dir() == fake_home / ".uterm"

    def test_default_host_key_dir_raises_when_no_home(self) -> None:
        from provide.uterm.transports.ssh import _default_host_key_dir

        with patch("provide.uterm.transports.ssh.Path.home", side_effect=RuntimeError("no home")):
            with pytest.raises(RuntimeError, match="cannot determine"):
                _default_host_key_dir()
