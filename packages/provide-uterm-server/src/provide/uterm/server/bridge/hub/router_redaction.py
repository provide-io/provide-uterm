#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Frame-field redaction helpers for the message router.

Pure, stateless helpers extracted from
:mod:`provide.uterm.server.bridge.hub.router_impl`. They take a
:class:`StreamRedactor` and return redacted copies of wire frames /
nested payloads; they never mutate their inputs and have no dependency
on :class:`MessageRouter` or the hub. ``router_impl`` re-exports
:func:`_redact_value` and :func:`_redact_frame_fields` so the public
``router_impl`` namespace (and the tests that import these from it) is
unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from provide.uterm.server.bridge.hub.redaction import StreamRedactor


# Defensive recursion bound for _redact_value. Wire frames are JSON-decoded
# (acyclic) and shallow in practice; this cap only guards against a
# pathologically deep structure causing a RecursionError. A value below the cap
# is returned as-is rather than redacted — secrets that deep don't occur in real
# snapshot/analysis payloads, and failing closed on depth (returning raw) is the
# conservative choice vs. raising on a hot broadcast path.
_REDACT_MAX_DEPTH = 32


def _redact_value(value: Any, redactor: StreamRedactor, _depth: int = 0) -> Any:
    """Recursively redact string values inside nested dict/list structures.

    Strings are redacted; dicts and lists are walked (values/elements only,
    not dict keys); all other scalars (int/float/bool/None) are returned
    unchanged. Recursion is capped at ``_REDACT_MAX_DEPTH`` — values deeper than
    that are returned verbatim. Input is never mutated; new containers are built.
    """
    if isinstance(value, str):
        return redactor.redact(value)
    if _depth >= _REDACT_MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _redact_value(v, redactor, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, redactor, _depth + 1) for v in value]
    return value


def _redact_frame_fields(msg: dict[str, Any], redactor: StreamRedactor) -> dict[str, Any]:
    """Return a copy of *msg* with its terminal-content string fields redacted.

    Applies role-scoped redaction to the content-bearing fields of term,
    snapshot, and analysis frames; other frame types are returned unchanged.
    """
    mtype = msg.get("type")
    if mtype == "term":
        return {**msg, "data": redactor.redact(str(msg.get("data", "")))}
    if mtype == "snapshot":
        out = dict(msg)
        out["screen"] = redactor.redact(str(msg.get("screen", "")))
        raw_tail = msg.get("raw_tail")
        if isinstance(raw_tail, str):
            out["raw_tail"] = redactor.redact(raw_tail)
        # prompt_detected is a dict that can carry the matched prompt text
        # (which may include secrets); redact its nested string values. None /
        # absent stays as-is (_redact_value returns scalars unchanged).
        if "prompt_detected" in out:
            out["prompt_detected"] = _redact_value(out["prompt_detected"], redactor)
        return out
    if mtype == "analysis":
        out = dict(msg)
        out["formatted"] = redactor.redact(str(msg.get("formatted", "")))
        raw = msg.get("raw")
        if isinstance(raw, str):
            out["raw"] = redactor.redact(raw)
        elif isinstance(raw, (dict, list)):
            # A structured raw was previously shipped verbatim; redact nested
            # string values so secrets inside dict/list raw don't egress.
            out["raw"] = _redact_value(raw, redactor)
        return out
    return msg


__all__ = ["_REDACT_MAX_DEPTH", "_redact_frame_fields", "_redact_value"]
