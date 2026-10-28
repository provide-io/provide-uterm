#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Tests for the capture-owner config keys on PamConfig.

Decision 1 of the capture-session-ownership ARD: a capture session's owner is
set when uterm creates it. The ``capture_owner`` key selects how: ``pam_user``
(default), ``hook`` (ask a URL), or ``none`` (today's behaviour, no owner).

These tests cover the config validation: the ``hook`` mode refuses to load
without a URL or a secret, the hook URL is held to https:// or loopback, and
the timeout is bounded 0.1 .. 5.0.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from provide.uterm.server.config_schema import PamConfig


class TestCaptureOwnerDefault:
    def test_the_default_is_pam_user(self) -> None:
        cfg = PamConfig()
        assert cfg.capture_owner == "pam_user"

    def test_pam_user_loads_without_a_hook_url_or_secret(self) -> None:
        cfg = PamConfig(capture_owner="pam_user")
        assert cfg.capture_owner_hook_url is None
        assert cfg.capture_owner_hook_secret is None

    def test_none_loads_without_a_hook_url_or_secret(self) -> None:
        cfg = PamConfig(capture_owner="none")
        assert cfg.capture_owner_hook_url is None


class TestHookModeRequiresUrlAndSecret:
    def test_hook_without_a_url_does_not_load(self) -> None:
        with pytest.raises(ValidationError, match="capture_owner_hook_url is required"):
            PamConfig(capture_owner="hook", capture_owner_hook_secret="s" * 32)

    def test_hook_without_a_secret_does_not_load(self) -> None:
        with pytest.raises(ValidationError, match="capture_owner_hook_secret is required"):
            PamConfig(capture_owner="hook", capture_owner_hook_url="http://127.0.0.1:8080/hook")

    def test_hook_with_a_url_and_secret_loads(self) -> None:
        cfg = PamConfig(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8080/hook",
            capture_owner_hook_secret="a" * 32,
        )
        assert cfg.capture_owner == "hook"
        assert cfg.capture_owner_hook_url == "http://127.0.0.1:8080/hook"


class TestHookUrlScheme:
    def test_a_cleartext_url_to_a_non_loopback_host_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="capture_owner_hook_url"):
            PamConfig(
                capture_owner="hook",
                capture_owner_hook_url="http://evil.example.com/hook",
                capture_owner_hook_secret="a" * 32,
            )

    def test_an_https_url_is_accepted(self) -> None:
        cfg = PamConfig(
            capture_owner="hook",
            capture_owner_hook_url="https://hook.example.com/hook",
            capture_owner_hook_secret="a" * 32,
        )
        assert cfg.capture_owner_hook_url == "https://hook.example.com/hook"

    def test_a_loopback_http_url_is_accepted(self) -> None:
        cfg = PamConfig(
            capture_owner="hook",
            capture_owner_hook_url="http://127.0.0.1:8787/internal/uterm/capture-owner",
            capture_owner_hook_secret="a" * 32,
        )
        assert cfg.capture_owner_hook_url == "http://127.0.0.1:8787/internal/uterm/capture-owner"

    def test_a_localhost_http_url_is_accepted(self) -> None:
        cfg = PamConfig(
            capture_owner="hook",
            capture_owner_hook_url="http://localhost:8787/hook",
            capture_owner_hook_secret="a" * 32,
        )
        assert cfg.capture_owner_hook_url == "http://localhost:8787/hook"


class TestHookTimeoutBounds:
    def test_the_default_timeout_is_one_second(self) -> None:
        cfg = PamConfig()
        assert cfg.capture_owner_hook_timeout_s == 1.0

    def test_the_minimum_timeout_is_accepted(self) -> None:
        cfg = PamConfig(capture_owner_hook_timeout_s=0.1)
        assert cfg.capture_owner_hook_timeout_s == 0.1

    def test_the_maximum_timeout_is_accepted(self) -> None:
        cfg = PamConfig(capture_owner_hook_timeout_s=5.0)
        assert cfg.capture_owner_hook_timeout_s == 5.0

    def test_a_timeout_below_the_minimum_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="capture_owner_hook_timeout_s"):
            PamConfig(capture_owner_hook_timeout_s=0.05)

    def test_a_timeout_above_the_maximum_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="capture_owner_hook_timeout_s"):
            PamConfig(capture_owner_hook_timeout_s=5.5)
