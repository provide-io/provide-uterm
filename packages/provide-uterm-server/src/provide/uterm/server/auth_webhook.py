#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Webhook IdP delegation and its replay-protection cache."""

from __future__ import annotations

import hmac
import secrets
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from provide.telemetry import get_logger
from provide.uterm.server.audit import audit_event as _direct_audit_event
from provide.uterm.server.auth_roles import _DEFAULT_ROLE, _filter_known_roles
from provide.uterm.server.bridge.identity import IdentityProvider, Principal, canonical_tenant_id
from provide.uterm.server.tracing import inject_trace_context

if TYPE_CHECKING:
    from typing import Any

    from fastapi import Request, WebSocket

logger = get_logger(__name__)


def audit_event(action: str, **kwargs: Any) -> None:
    """Emit an audit event, honouring a monkeypatch on the ``auth`` facade.

    The webhook IdP historically lived in ``provide.uterm.server.auth`` and
    tests patch ``auth.audit_event`` to spy on the failure path. Resolving the
    callable through that facade at call time (with a lazy import to avoid a
    module-load cycle) keeps those patches effective now that the code lives
    here, while plain calls dispatch to the real audit sink.
    """
    from provide.uterm.server import auth as _auth_facade

    sink = getattr(_auth_facade, "audit_event", _direct_audit_event)
    sink(action, **kwargs)


_REPLAY_CACHE_MAX_ENTRIES = 4096


class _BoundedReplayCache:
    """Bounded, TTL-evicted cache of recently-seen response signatures.

    L9 replay protection (layer 1): records each verified IdP response
    ``signature`` against the wall-clock time it was first seen. A signature
    presented again while its first-seen timestamp is still within the
    freshness window (``max_age_s`` — the same window the response-signature
    timestamp check uses) is reported as a replay.

    Bounded two ways so an attacker cannot grow it without limit: stale
    entries (older than ``max_age_s``) are purged on every insert, and the
    structure is hard-capped at ``max_entries`` with oldest-first eviction
    (``OrderedDict`` FIFO). A signature is only meaningful for one freshness
    window anyway, so eviction never weakens protection — a replay outside the
    window is independently rejected by the signature timestamp check.

    HA caveat: this cache is PER PROVIDER INSTANCE (per process). It does not
    protect a multi-node / HA deployment where a captured response could be
    replayed against a *different* instance. The optional nonce binding
    (``require_response_nonce``) covers that case by binding the response to a
    per-request nonce.
    """

    def __init__(self, max_age_s: float, max_entries: int = _REPLAY_CACHE_MAX_ENTRIES) -> None:
        self._max_age_s = max_age_s
        self._max_entries = max_entries
        # signature -> first-seen wall-clock timestamp; FIFO for eviction.
        self._seen: OrderedDict[str, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, signature: str) -> bool:
        return signature in self._seen

    def _purge_stale(self, now: float) -> None:
        # Entries are inserted in time order, so the oldest live at the front;
        # stop at the first entry still within the window.
        for sig in list(self._seen):
            if now - self._seen[sig] > self._max_age_s:
                del self._seen[sig]
            else:
                break

    def seen_or_record(self, signature: str, *, now: float) -> bool:
        """Return True if ``signature`` is a replay; otherwise record it.

        A signature counts as a replay when it was already recorded and its
        first-seen timestamp is within ``max_age_s`` of ``now``. Stale entries
        are purged first, so a signature whose window has elapsed is treated as
        fresh (and re-recorded).
        """
        self._purge_stale(now)
        if signature in self._seen and now - self._seen[signature] <= self._max_age_s:
            return True
        self._seen[signature] = now
        self._seen.move_to_end(signature)
        # Hard cap: evict oldest entries beyond the bound.
        while len(self._seen) > self._max_entries:
            self._seen.popitem(last=False)
        return False


class WebhookIdentityProvider(IdentityProvider):
    """IdentityProvider that delegates resolution to an external webhook."""

    def __init__(
        self,
        url: str,
        secret: str | None = None,
        timeout_s: float = 2.0,
        on_failure: str = "deny",
        require_signed_response: bool = True,
        forward_headers: frozenset[str] | None = None,
        forward_cookies: frozenset[str] | None = None,
        require_response_nonce: bool = False,
    ):
        self.url = url
        self.secret = secret
        self.timeout_s = timeout_s
        # Finding #7: webhook-down behaviour.  ``"deny"`` (the default) returns
        # ``None`` so the caller falls through to anonymous and the request is
        # rejected by the auth gate.  ``"viewer"`` preserves the legacy
        # fail-open behaviour for callers that explicitly want it.
        if on_failure not in {"deny", "viewer"}:
            raise ValueError(f"on_failure must be 'deny' or 'viewer'; got {on_failure!r}")
        self.on_failure = on_failure
        # 1f: when True, the webhook's RESPONSE must carry a valid HMAC signature
        # (over the raw response bytes) or the resolution falls into ``on_failure``.
        self.require_signed_response = require_signed_response
        # 1d: only these request headers/cookies are forwarded to the external
        # IdP — never the full request set. Empty = forward nothing (secure
        # default); the factory passes the curated auth-credential allow-list.
        self.forward_headers = forward_headers if forward_headers is not None else frozenset()
        self.forward_cookies = forward_cookies if forward_cookies is not None else frozenset()
        # L9 layer 3: when True, the IdP RESPONSE must echo the per-request nonce
        # (matching) or the resolution falls into on_failure. Defaults False for
        # backward-compat — an IdP that doesn't echo the nonce is still protected
        # by the per-instance replay cache (layer 1). Set True for HA / strict
        # request↔response binding where the per-instance cache isn't shared.
        self.require_response_nonce = require_response_nonce
        # L9 layer 1: per-instance bounded replay cache keyed on the response
        # signature, windowed to the same freshness horizon the signature check
        # uses so the two reject overlapping (but together cover the full
        # threat: in-window verbatim replay AND out-of-window stale timestamp).
        from provide.uterm.server.webhook_signing import _DEFAULT_MAX_AGE_S

        self._replay_cache = _BoundedReplayCache(max_age_s=_DEFAULT_MAX_AGE_S)

    async def resolve_principal(self, connection: Request | WebSocket) -> Principal | None:
        import json

        import httpx

        from provide.uterm.server.webhook_signing import build_webhook_signature, verify_webhook_signature

        all_headers = dict(getattr(connection, "headers", {}))
        all_cookies = dict(getattr(connection, "cookies", {}))

        # 1d: forward only the curated allow-list of credentials. Header keys are
        # matched case-insensitively (Starlette/httpx lower-case keys, but a
        # mixed-case mapping may reach us in tests/embedders); cookies match by
        # exact name.
        headers = {k: v for k, v in all_headers.items() if k.lower() in self.forward_headers}
        cookies = {k: v for k, v in all_cookies.items() if k in self.forward_cookies}

        # L9 layer 2: a fresh per-request nonce, carried BOTH inside the signed
        # request payload (so the request signature covers it) and as the
        # X-Uterm-Nonce header (so a non-validating IdP can echo it without
        # parsing the body). If the IdP echoes it in its (signed) response, we
        # verify the echo matches — cryptographically binding the response to
        # THIS request, defeating replay even across instances/HA.
        nonce = secrets.token_urlsafe(16)
        payload = {
            "headers": headers,
            "cookies": cookies,
            "action": "resolve_principal",
            "nonce": nonce,
        }

        body = json.dumps(payload, separators=(",", ":")).encode()
        req_headers: dict[str, str] = {"Content-Type": "application/json", "X-Uterm-Nonce": nonce}
        if self.secret:
            ts = str(time.time())
            req_headers["X-Uterm-Timestamp"] = ts
            req_headers["X-Uterm-Signature"] = build_webhook_signature(self.secret, body, ts)
        # Propagate the active W3C trace context onto the IDP resolution call so
        # the auth hop joins the same distributed trace. Via provide.telemetry
        # (OpenTelemetry-optional) — no-op when no span is active.
        inject_trace_context(req_headers)

        try:
            from provide.uterm.server.egress import assert_webhook_target_allowed

            await assert_webhook_target_allowed(self.url)
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(self.url, content=body, headers=req_headers)
                resp.raise_for_status()

                # 1f: authenticate the RESPONSE itself. Verify the HMAC signature
                # over the RAW response bytes (resp.content) BEFORE trusting any
                # of its fields — a MITM/compromised transport could otherwise
                # forge a principal. A failed check raises into the except below
                # so the on_failure (deny/viewer) + audit path fires. The
                # principal is then built from json.loads(resp.content) so the
                # parsed data and the verified bytes can never diverge.
                if self.require_signed_response and not verify_webhook_signature(
                    self.secret or "",
                    resp.content,
                    resp.headers.get("X-Uterm-Signature"),
                    resp.headers.get("X-Uterm-Timestamp"),
                ):
                    raise ValueError("webhook IdP response signature verification failed")

                # L9 layer 1: reject a verbatim replay. A signature is only valid
                # for one freshness window, so an attacker can only replay a
                # captured (signature, timestamp) pair while the signature check
                # above still accepts it — exactly the window this cache covers.
                # The first delivery records the signature; an identical signature
                # presented again within the window is a replay → rejected. (Skip
                # when there is no signature header to key on; the verbatim-replay
                # vector only exists once a response is signed.)
                resp_sig = resp.headers.get("X-Uterm-Signature")
                if resp_sig and self._replay_cache.seen_or_record(resp_sig, now=time.time()):
                    raise ValueError("webhook IdP response replay detected")

                data = json.loads(resp.content)

                # L9 layer 2/3: bind the response to this request via the nonce.
                # ``echoed`` is what the IdP returned in its signed body.
                #   * require_response_nonce=True → the echo MUST be present and
                #     match (else reject) — strict request↔response binding.
                #   * require_response_nonce=False → a present echo must still
                #     match (a present-but-wrong nonce is an attack → reject); an
                #     absent echo falls through to the replay-cache protection.
                echoed = data.get("nonce")
                if self.require_response_nonce:
                    if echoed is None or not hmac.compare_digest(str(echoed), nonce):
                        raise ValueError("webhook IdP response nonce missing or mismatched")
                elif echoed is not None and not hmac.compare_digest(str(echoed), nonce):
                    raise ValueError("webhook IdP response nonce mismatched")

                # Filter roles to the known allow-list: a compromised or
                # MITM'd IDP webhook must not be able to mint a privileged role
                # (e.g. admin) outside the recognised set.
                return Principal(
                    subject_id=data["subject_id"],
                    tenant_id=canonical_tenant_id(data.get("tenant_id")),
                    roles=_filter_known_roles(data.get("roles", [_DEFAULT_ROLE])),
                    scopes=frozenset(data.get("scopes", [])),
                    claims=data.get("claims", {}),
                    display_name=data.get("display_name"),
                )
        except Exception as exc:
            logger.warning(
                "webhook_auth_failed url=%s error=%s on_failure=%s",
                self.url,
                exc,
                self.on_failure,
            )
            # Surface the fail-open/attack signal in the structured audit
            # trail. Deliberately exclude the signing secret and raw request
            # headers from the detail so they never reach the audit log.
            audit_event(
                "auth.webhook_idp_failure",
                detail={"url": self.url, "on_failure": self.on_failure, "error": str(exc)},
            )
            if self.on_failure == "viewer":
                return Principal.anonymous()
            return None
