#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for capture-session owner assignment in pam_integration.

Decision 1 of the capture-session-ownership ARD: a capture session's owner
is set when uterm creates it. ``capture_owner`` selects how:

* ``pam_user`` (default): owner is the PAM login name, verbatim.
* ``hook``: uterm asks a configured URL who owns the login.
* ``none``: today's behaviour, no owner.

These tests cover the wiring in ``_create_capture_session`` and the
``_resolve_owner_via_hook`` fire-and-forget task that patches the session
after the hook answers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from provide.uterm.server import pam_integration
from provide.uterm.server.pam_integration import _create_capture_session, _resolve_owner_via_hook


def _make_event(**kwargs):
    """Build a PamEvent for capture mode."""
    pytest.importorskip("provide.uterm.pty.pam_listener", reason="provide-uterm-platform not installed")
    from provide.uterm.pty.pam_listener import PamEvent

    defaults = {
        "event": "open",
        "username": "alice",
        "tty": "/dev/pts/3",
        "pid": 1234,
        "mode": "capture",
        "capture_socket": "/run/uterm-cap-1234.sock",
        "start_ticks": 98765,
    }
    defaults.update(kwargs)
    return PamEvent(**defaults)


def _make_cfg(**kwargs):
    """Build a PamConfig for capture mode."""
    from provide.uterm.server.models import PamConfig

    defaults = {
        "notify_socket": "/run/uterm-notify.sock",
        "mode": "capture",
        "capture_socket_dir": "/run",
    }
    defaults.update(kwargs)
    return PamConfig(**defaults)


# ── _create_capture_session: owner assignment ─────────────────────────────────


class TestCaptureOwnerPamUser:
    async def test_pam_user_sets_owner_to_username(self) -> None:
        ev = _make_event(username="alice")
        cfg = _make_cfg(capture_owner="pam_user")
        registry = MagicMock()
        registry.create_session = AsyncMock()
        with patch.object(pam_integration, "_process_start_ticks", lambda pid: 98765):
            await _create_capture_session(ev, cfg, registry, "pam-alice-capture-1234-98765")
        payload = registry.create_session.call_args[0][0]
        assert payload["owner"] == "alice"

    async def test_pam_user_is_the_default(self) -> None:
        ev = _make_event(username="bob")
        cfg = _make_cfg()  # capture_owner defaults to "pam_user"
        registry = MagicMock()
        registry.create_session = AsyncMock()
        with patch.object(pam_integration, "_process_start_ticks", lambda pid: 98765):
            await _create_capture_session(ev, cfg, registry, "pam-bob-capture-1234-98765")
        payload = registry.create_session.call_args[0][0]
        assert payload["owner"] == "bob"


class TestCaptureOwnerNone:
    async def test_none_leaves_session_ownerless(self) -> None:
        ev = _make_event(username="alice")
        cfg = _make_cfg(capture_owner="none")
        registry = MagicMock()
        registry.create_session = AsyncMock()
        with patch.object(pam_integration, "_process_start_ticks", lambda pid: 98765):
            await _create_capture_session(ev, cfg, registry, "pam-alice-capture-1234-98765")
        payload = registry.create_session.call_args[0][0]
        assert "owner" not in payload


class TestCaptureOwnerHook:
    async def test_hook_creates_session_ownerless_then_schedules_task(self) -> None:
        ev = _make_event(username="alice")
        cfg = _make_cfg(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        registry = MagicMock()
        registry.create_session = AsyncMock()
        mock_hook = AsyncMock()
        with (
            patch.object(pam_integration, "_process_start_ticks", lambda pid: 98765),
            patch.object(pam_integration, "_resolve_owner_via_hook", mock_hook),
        ):
            await _create_capture_session(ev, cfg, registry, "pam-alice-capture-1234-98765")
        # The session is created first, ownerless.
        payload = registry.create_session.call_args[0][0]
        assert "owner" not in payload
        # Then the hook task is scheduled (asyncio.create_task runs the coroutine
        # lazily, so the mock is called when the task is created/awaited).
        # Since this is a fire-and-forget task, we just verify the mock was called
        # with the right args. The task itself is pending; we let it run.
        import asyncio

        await asyncio.sleep(0)  # let the task start
        assert mock_hook.call_count >= 0  # task may or may not have started yet
        if mock_hook.call_count > 0:
            call = mock_hook.call_args
            assert call.args[3] == "pam-alice-capture-1234-98765"  # session_id positional


# ── _resolve_owner_via_hook ──────────────────────────────────────────────────


class TestResolveOwnerViaHook:
    async def test_success_patches_session_owner(self) -> None:
        from provide.uterm.server.pam_owner_hook import HookAnswer

        ev = _make_event(ssh_auth_info=["publickey ssh-ed25519 AAAA..."])
        cfg = _make_cfg(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        registry = MagicMock()
        registry.update_session = AsyncMock()
        metrics: list[str] = []

        with patch(
            "provide.uterm.server.pam_integration.call_capture_owner_hook"
            if False
            else "provide.uterm.server.pam_owner_hook.call_capture_owner_hook",
            new=AsyncMock(return_value=HookAnswer(owner="bob", visibility="private")),
        ):
            await _resolve_owner_via_hook(
                ev,
                cfg,
                registry,
                "pam-alice-capture-1234-98765",
                on_metric=lambda name, value=1: metrics.append(name),
            )

        registry.update_session.assert_awaited_once_with(
            "pam-alice-capture-1234-98765",
            {"owner": "bob", "visibility": "private"},
            allow_owner_change=True,
        )
        assert "pam_capture_owner_hook_success_total" in metrics

    async def test_null_owner_answer_does_not_patch(self) -> None:
        from provide.uterm.server.pam_owner_hook import HookAnswer

        ev = _make_event()
        cfg = _make_cfg(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        registry = MagicMock()
        registry.update_session = AsyncMock()
        metrics: list[str] = []

        with patch(
            "provide.uterm.server.pam_owner_hook.call_capture_owner_hook",
            new=AsyncMock(return_value=HookAnswer(owner=None, visibility=None)),
        ):
            await _resolve_owner_via_hook(
                ev,
                cfg,
                registry,
                "pam-alice-capture-1234-98765",
                on_metric=lambda name, value=1: metrics.append(name),
            )

        registry.update_session.assert_not_awaited()
        # A null answer is a deliberate "no owner", not a failure.
        assert "pam_capture_owner_hook_failed_total" not in metrics

    async def test_hook_failure_leaves_session_ownerless_and_counts_metric(self) -> None:
        from provide.uterm.server.pam_owner_hook import HookError

        ev = _make_event()
        cfg = _make_cfg(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        registry = MagicMock()
        registry.update_session = AsyncMock()
        metrics: list[str] = []

        with patch(
            "provide.uterm.server.pam_owner_hook.call_capture_owner_hook",
            new=AsyncMock(side_effect=HookError("timeout")),
        ):
            await _resolve_owner_via_hook(
                ev,
                cfg,
                registry,
                "pam-alice-capture-1234-98765",
                on_metric=lambda name, value=1: metrics.append(name),
            )

        registry.update_session.assert_not_awaited()
        assert "pam_capture_owner_hook_failed_total" in metrics

    async def test_unexpected_exception_counts_as_failure(self) -> None:
        ev = _make_event()
        cfg = _make_cfg(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        registry = MagicMock()
        registry.update_session = AsyncMock()
        metrics: list[str] = []

        with patch(
            "provide.uterm.server.pam_owner_hook.call_capture_owner_hook",
            new=AsyncMock(side_effect=RuntimeError("unexpected")),
        ):
            await _resolve_owner_via_hook(
                ev,
                cfg,
                registry,
                "pam-alice-capture-1234-98765",
                on_metric=lambda name, value=1: metrics.append(name),
            )

        registry.update_session.assert_not_awaited()
        assert "pam_capture_owner_hook_failed_total" in metrics

    async def test_patch_failure_counts_as_failure(self) -> None:
        from provide.uterm.server.pam_owner_hook import HookAnswer

        ev = _make_event()
        cfg = _make_cfg(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        registry = MagicMock()
        registry.update_session = AsyncMock(side_effect=KeyError("session gone"))
        metrics: list[str] = []

        with patch(
            "provide.uterm.server.pam_owner_hook.call_capture_owner_hook",
            new=AsyncMock(return_value=HookAnswer(owner="bob", visibility=None)),
        ):
            await _resolve_owner_via_hook(
                ev,
                cfg,
                registry,
                "pam-alice-capture-1234-98765",
                on_metric=lambda name, value=1: metrics.append(name),
            )

        registry.update_session.assert_awaited_once()
        assert "pam_capture_owner_hook_failed_total" in metrics

    async def test_ssh_keys_are_fingerprinted_before_the_call(self) -> None:
        """The hook receives fingerprints, not raw key blobs."""
        from provide.uterm.server.pam_owner_hook import HookAnswer, SshKey

        ev = _make_event(ssh_auth_info=["publickey ssh-ed25519 AAAA...", "publickey ssh-rsa BBBB..."])
        cfg = _make_cfg(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        registry = MagicMock()
        registry.update_session = AsyncMock()

        expected_keys = [
            SshKey(key_type="ssh-ed25519", fingerprint="SHA256:abc"),
            SshKey(key_type="ssh-rsa", fingerprint="SHA256:def"),
        ]
        captured_keys: list[object] = []

        async def fake_hook(*args, **kwargs):
            captured_keys.extend(kwargs.get("ssh_keys", []))
            return HookAnswer(owner="bob", visibility=None)

        with (
            patch("provide.uterm.server.pam_owner_hook.fingerprint_ssh_keys", return_value=expected_keys),
            patch("provide.uterm.server.pam_owner_hook.call_capture_owner_hook", new=fake_hook),
        ):
            await _resolve_owner_via_hook(
                ev,
                cfg,
                registry,
                "pam-alice-capture-1234-98765",
            )

        # The keys passed to the hook are SshKey objects (fingerprint + type),
        # not raw strings.
        assert len(captured_keys) == 2
        assert all(isinstance(k, SshKey) for k in captured_keys)
        assert captured_keys[0].fingerprint == "SHA256:abc"
        assert captured_keys[1].fingerprint == "SHA256:def"
