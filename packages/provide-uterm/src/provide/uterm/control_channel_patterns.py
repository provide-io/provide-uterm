#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Server-side registry for ``link_patterns`` control channel frames.

``LinkPattern`` is an immutable value object representing one server-driven
clickable text decoration (see ``xterm-server-links.js`` for the wire format).

``LinkPatternRegistry`` tracks the currently-active set of patterns for a
single owner (session, worker, etc.).  Callers are responsible for creating
one registry per owner — there is no shared global state.

Thread / async safety
---------------------
Registry mutation methods (``register``, ``unregister``, ``clear``) mutate a
plain ``dict``.  CPython's GIL makes individual ``dict`` operations atomic, so
single-owner concurrent use from multiple coroutines (or threads) is safe for
the simple register/unregister/clear/get_all/sync_payload operations implemented
here.  If you need compound-atomic sequences (check-then-act), add an
``asyncio.Lock`` in your own calling code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

_VALID_ACTIONS: frozenset[str] = frozenset({"cmd", "url", "key", "focus"})


@dataclass(frozen=True, slots=True)
class LinkPattern:
    """An immutable descriptor for one server-driven clickable text pattern.

    Parameters
    ----------
    pattern:
        JavaScript regex source string (e.g. ``r"\\((\\d{1,5})\\)"``).
    action:
        What happens when the user clicks: ``"cmd"``, ``"url"``, ``"key"``,
        or ``"focus"``.
    id:
        Optional stable identifier used by :meth:`LinkPatternRegistry.unregister`.
        If two patterns share the same *id* the later ``register`` call silently
        replaces the earlier one.
    flags:
        Regex flags forwarded to ``new RegExp(pattern, flags)``.  Defaults to
        ``"g"``; ``"g"`` is always ensured on the client side regardless.
    group:
        Which capture group is the clickable span (0 = whole match).
    payload:
        Click payload template; ``$1``, ``$2``, … are substituted from captures.
    hover:
        Hover tooltip template (same ``$N`` substitution).
    class_:
        CSS class name applied to highlighted ranges.  Serialised as ``"class"``
        in the wire frame (``class`` is a Python reserved word).
    """

    pattern: str
    action: Literal["cmd", "url", "key", "focus"]
    id: str | None = None
    flags: str = "g"
    group: int = 0
    payload: str = ""
    hover: str = ""
    class_: str = ""

    def __post_init__(self) -> None:
        if self.action not in _VALID_ACTIONS:
            raise ValueError(f"invalid action {self.action!r}; must be one of {sorted(_VALID_ACTIONS)}")

    def to_frame_entry(self) -> dict[str, Any]:
        """Serialise to the wire-format dict expected by ``xterm-server-links.js``.

        Only non-default / non-empty optional fields are included to keep
        frames compact.  ``class_`` is emitted as ``"class"``.
        """
        entry: dict[str, Any] = {
            "pattern": self.pattern,
            "action": self.action,
        }
        if self.id is not None:
            entry["id"] = self.id
        if self.flags != "g":
            entry["flags"] = self.flags
        if self.group != 0:
            entry["group"] = self.group
        if self.payload:
            entry["payload"] = self.payload
        if self.hover:
            entry["hover"] = self.hover
        if self.class_:
            entry["class"] = self.class_
        return entry


@dataclass
class LinkPatternRegistry:
    """Active pattern set for one owner (session, worker, etc.).

    Patterns are stored in insertion order.  Registering a pattern whose ``id``
    already exists **replaces** the earlier pattern in-place (preserving the
    slot's position so that order is predictable for callers who ``register``
    once and later refresh the same id).

    Patterns without an ``id`` (``id=None``) are appended and cannot be removed
    individually; use :meth:`clear` to reset the whole set.
    """

    # Internal store: key → pattern.  For id-less patterns the key is a
    # monotonically increasing sentinel so they never collide.
    _patterns: dict[str | int, LinkPattern] = field(default_factory=dict, init=False, repr=False)
    _counter: int = field(default=0, init=False, repr=False)

    def register(self, pattern: LinkPattern) -> None:
        """Add *pattern* to the active set.

        If *pattern* has an ``id`` and that id is already registered the
        existing entry is replaced (same dict slot, so insertion order is
        preserved for that id).  If ``id`` is ``None`` the pattern is appended
        unconditionally.
        """
        if pattern.id is not None:
            self._patterns[pattern.id] = pattern
        else:
            self._patterns[self._counter] = pattern
            self._counter += 1

    def unregister(self, pattern_id: str) -> bool:
        """Remove the pattern registered under *pattern_id*.

        Returns ``True`` if the pattern was found and removed, ``False`` if no
        pattern with that id exists.
        """
        if pattern_id in self._patterns:
            del self._patterns[pattern_id]
            return True
        return False

    def clear(self) -> None:
        """Remove all patterns and reset the id-less counter."""
        self._patterns.clear()
        self._counter = 0

    def get_all(self) -> list[LinkPattern]:
        """Return all active patterns in insertion order."""
        return list(self._patterns.values())

    def sync_payload(self) -> dict[str, Any]:
        """Return the ready-to-send dict for :func:`~provide.uterm.control_channel.encode_control_frame`.

        The returned dict has the shape::

            {"type": "link_patterns", "patterns": [...]}

        Calling this method is non-destructive; the registry state is unchanged.
        """
        return {
            "type": "link_patterns",
            "patterns": [p.to_frame_entry() for p in self._patterns.values()],
        }
