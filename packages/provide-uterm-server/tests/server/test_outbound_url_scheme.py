#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
from __future__ import annotations

import pytest

from provide.uterm.server.config_schema import AuthConfig, GovernanceConfig


def test_cleartext_governance_url_to_remote_host_is_rejected() -> None:
    with pytest.raises(ValueError, match="https"):
        GovernanceConfig(policy_webhook_url="http://policy.internal/decide")


def test_https_governance_url_is_accepted() -> None:
    cfg = GovernanceConfig(policy_webhook_url="https://policy.internal/decide")
    assert cfg.policy_webhook_url == "https://policy.internal/decide"


def test_loopback_http_governance_url_is_allowed_for_dev() -> None:
    cfg = GovernanceConfig(authz_webhook_url="http://127.0.0.1:9000/authz")
    assert cfg.authz_webhook_url == "http://127.0.0.1:9000/authz"


def test_cleartext_idp_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="https"):
        AuthConfig(webhook_idp_url="http://idp.internal/resolve")


def test_localhost_http_idp_url_allowed() -> None:
    cfg = AuthConfig(webhook_idp_url="http://localhost:8080/resolve")
    assert cfg.webhook_idp_url == "http://localhost:8080/resolve"


def test_non_http_scheme_rejected() -> None:
    with pytest.raises(ValueError, match="http"):
        GovernanceConfig(policy_webhook_url="ftp://policy.internal/x")
