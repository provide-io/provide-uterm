#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Unified security-posture self-report for the hosted terminal server.

``compute_security_posture`` collapses the ~8 independent security knobs the
server has accumulated into a single JSON-safe dict describing the *effective*
posture of a running configuration: which dev opt-outs are active, what bind
host is in use, and one boolean ``secure`` summary. It is a pure function (no
I/O) so it can be logged at startup and served from an auth-gated endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from provide.uterm.server.app.auth import _is_loopback_host

if TYPE_CHECKING:
    from provide.uterm.server.models import ServerConfig


def compute_security_posture(config: ServerConfig) -> dict[str, object]:
    """Return a JSON-safe dict describing the effective security posture.

    Pure function: reads only ``config`` and returns plain JSON types. Each
    entry in ``dev_opt_outs`` names an *active, security-weakening* opt-out.

    A knob counts as "weakening" only when it is actually relaxing a control:

      * ``auth.mode=dev_token`` — stub-IdP dev auth (startup-gated to loopback).
      * ``security.mode=dev`` — strips HSTS/CSP/X-Frame-Options (startup-gated
        to loopback unless acknowledged).
      * ``auth.webhook_idp_on_failure=viewer`` — fail-open to an anonymous
        viewer principal when the webhook IdP errors.
      * ``auth.header_mode_acknowledged`` — only weakening when paired with
        ``auth.mode=header`` (it acknowledges trusting X-Uterm-Role from
        callers); inert otherwise.
      * ``security.dev_mode_acknowledged`` — only weakening when paired with
        ``security.mode=dev`` (acknowledges relaxed headers on a routable
        bind); inert otherwise.
      * ``auth.allow_adhoc_browser_observers`` — lets non-admins observe
        unregistered (ad-hoc) workers.
      * ``security.block_private_connector_targets=false`` — connectors may
        reach internal/loopback/link-local hosts. Default-False is the tool's
        intended use, but in posture terms it is a relaxation worth surfacing.
    """
    bind_host = str(config.server.host).strip().lower()
    is_loopback = _is_loopback_host(bind_host)
    # ``dev_token`` collapses to ``jwt`` once setup_dev_idp runs at startup. The
    # declared mode is stamped on the AuthConfig by that path; prefer it so the
    # posture report surfaces the dev opt-out rather than the post-mutation jwt.
    auth_mode = str(getattr(config.auth, "_declared_auth_mode", None) or config.auth.mode).strip().lower()
    security_mode = str(config.security.mode).strip().lower()

    dev_opt_outs: list[str] = []
    warnings: list[str] = []

    if auth_mode == "dev_token":
        dev_opt_outs.append("auth.mode=dev_token")
        warnings.append("dev_token auth on a non-loopback bind is refused at startup")

    if security_mode == "dev":
        dev_opt_outs.append("security.mode=dev")
        warnings.append("security.mode=dev strips HSTS/CSP/X-Frame-Options")
        # dev_mode_acknowledged only weakens posture when it actually unlocks
        # the relaxed header set (i.e. security.mode=dev).
        if config.security.dev_mode_acknowledged:
            dev_opt_outs.append("security.dev_mode_acknowledged")

    if str(getattr(config.auth, "webhook_idp_on_failure", "deny")).strip().lower() == "viewer":
        dev_opt_outs.append("auth.webhook_idp_on_failure=viewer")
        warnings.append("anonymous-viewer IDP fallback is enabled (webhook_idp_on_failure=viewer)")

    # 1f: webhook IdP without response-signature verification → a forged response
    # can mint a principal. Only a weakening opt-out for the webhook provider.
    identity_provider = str(getattr(config.auth, "identity_provider", "local")).strip().lower()
    require_signed_response = bool(getattr(config.auth, "webhook_idp_require_signed_response", True))
    if identity_provider == "webhook" and not require_signed_response:
        dev_opt_outs.append("auth.webhook_idp_require_signed_response=false")
        warnings.append("webhook IdP responses are not signature-verified — a forged response can mint a principal")

    # header_mode_acknowledged only weakens posture in header auth mode.
    if auth_mode == "header" and config.auth.header_mode_acknowledged:
        dev_opt_outs.append("auth.header_mode_acknowledged")
        warnings.append("header auth trusts X-Uterm-Role headers from callers")

    if config.auth.allow_adhoc_browser_observers:
        dev_opt_outs.append("auth.allow_adhoc_browser_observers")
        warnings.append("non-admins may observe unregistered (ad-hoc) workers")

    if not config.security.block_private_connector_targets:
        dev_opt_outs.append("security.block_private_connector_targets=false (connectors may reach internal hosts)")

    # Compliance posture: the tamper-evident WORM audit chain. Defensive getattr
    # keeps the report working for embedder configs predating the ``audit`` field.
    audit_chain_enabled = bool(getattr(getattr(config, "audit", None), "chain_enabled", False))
    if not audit_chain_enabled:
        # Not a dev opt-out (it doesn't relax a control) — a compliance note: the
        # audit log is only a plain log stream, not integrity-chained.
        warnings.append(
            "audit log is not tamper-evident (audit.chain_enabled=false) — enable the WORM chain for compliance"
        )

    dev_opt_outs.sort()

    # ``secure`` is True iff this is a declared production deployment AND no
    # weakening opt-out is dangerous on this bind. A weakening opt-out is only
    # dangerous when the listener is remotely reachable: on a loopback bind the
    # deployment cannot be reached off-box, so loopback-only relaxations don't
    # demote the posture. Hence: production + (loopback OR no active opt-out).
    secure = config.environment == "production" and (is_loopback or not dev_opt_outs)

    return {
        "environment": str(config.environment),
        "bind_host": bind_host,
        "is_loopback": is_loopback,
        "auth_mode": auth_mode,
        "dev_opt_outs": dev_opt_outs,
        # 1f/1d: whether webhook-IDP response signing is required (real field,
        # secure-by-default True). Defensive getattr keeps the report JSON-safe
        # for any embedder config object that predates the field.
        "idp_signing_required": getattr(config.auth, "webhook_idp_require_signed_response", None),
        # Compliance: whether the tamper-evident WORM audit chain is enabled.
        "audit_chain_enabled": audit_chain_enabled,
        "warnings": warnings,
        "secure": secure,
    }
