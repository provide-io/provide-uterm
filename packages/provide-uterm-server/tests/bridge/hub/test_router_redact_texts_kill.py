#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Mutation kill-suite for ``MessageRouter.redact_texts_for_recipient``.

The previous-session read redacts an ended session's screen with the policy
the live screen is redacted by. What it asks that policy is pinned here: the
recipient, the session, and the "output" action, exactly as the live
broadcast asks, so a policy that decides by session or action answers the same.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from provide.uterm.server.bridge.hub.ext import RedactionRule
from provide.uterm.server.bridge.hub.router_impl import MessageRouter


def _router(rules: list[RedactionRule]) -> tuple[MessageRouter, MagicMock]:
    hub = MagicMock()
    hub._output_policy_gate.get_redaction_rules = AsyncMock(return_value=rules)
    hub.prepare_policy_context = AsyncMock(return_value="the context")
    return MessageRouter(hub), hub


async def test_the_policy_is_asked_about_the_recipient_the_session_and_output() -> None:
    router, hub = _router([RedactionRule(pattern="secret", replacement="[R]")])
    recipient = object()
    out = await router.redact_texts_for_recipient("w1", ["a secret", "plain"], recipient)
    assert out == ["a [R]", "plain"]
    hub.prepare_policy_context.assert_awaited_once_with(recipient, "w1", action="output")
    hub._output_policy_gate.get_redaction_rules.assert_awaited_once_with("the context")


async def test_no_rules_leaves_the_texts_as_they_are() -> None:
    router, _hub = _router([])
    texts = ["a secret"]
    assert await router.redact_texts_for_recipient("w1", texts, object()) is texts


async def test_no_gate_asks_nothing() -> None:
    router, hub = _router([])
    hub._output_policy_gate = None
    texts = ["a secret"]
    assert await router.redact_texts_for_recipient("w1", texts, object()) is texts
    hub.prepare_policy_context.assert_not_awaited()
