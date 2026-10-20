#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""How a hosted session runtime ends one worker connection's recording.

Split out of :mod:`provide.uterm.server.runtime` to keep that module from
growing past the repository's line limit; :class:`HostedSessionRuntime` takes
these methods as a mixin, unchanged, and they log through the runtime's logger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio

    from provide.uterm.server.models import SessionDefinition


def _logger() -> Any:
    """The runtime module's logger, looked up when used: it is the one that module and its tests own."""
    from provide.uterm.server import runtime

    return runtime.logger


class ConnectionRecordingMixin:
    """The end of a connection's recording, for :class:`~provide.uterm.server.runtime.HostedSessionRuntime`."""

    if TYPE_CHECKING:
        definition: SessionDefinition
        _stop: asyncio.Event
        _last_error: str | None
        _pending_close_failure: Exception | None

        async def _stop_recording(self) -> None: ...

        async def _log_event(self, event: str, payload: dict[str, Any]) -> None: ...

    async def _end_connection_recording(self, connection_error: str | None = None) -> None:
        """Close the recording at the end of one worker connection, never raising.

        A failed final flush here used to escape ``_run``'s ``finally`` and end
        the task: the session died silently and its connector stayed alive with
        nobody to stop it. Now the loop always carries on -- reconnecting, or,
        when stopping, falling through to cleanup -- and the failure is:

        * logged (``hosted_session_recording_close_failed``);
        * recorded in ``_last_error`` WITHOUT losing the error that ended the
          connection: *connection_error* (``str`` of it, or None after a clean
          end) is kept first and the close failure appended as
          ``"<connection error>; recording close failed: <close error>"``, or
          ``"recording close failed: <close error>"`` alone;
        * when the loop is being stopped (``_stop`` set), also kept in
          ``_pending_close_failure`` for ``stop()`` to raise after cleanup. A
          close failure on an ordinary reconnect is not raised anywhere.

        No ``runtime_error`` event is written: the recording it would go to is
        the one that just failed to close (``_stop_recording`` has dropped it).
        """

        try:
            await self._stop_recording()
        except Exception as exc:
            _logger().warning(
                "hosted_session_recording_close_failed session_id=%s error=%s",
                self.definition.session_id,
                exc,
            )
            close_error = f"recording close failed: {exc}"
            self._last_error = f"{connection_error}; {close_error}" if connection_error else close_error
            if self._stop.is_set():
                self._pending_close_failure = exc

    async def _log_runtime_error(self, payload: dict[str, Any]) -> None:
        """Write a ``runtime_error`` event to the recording, best-effort.

        The recording is often the very thing that failed (a store that keeps
        failing re-raises on every write, since the logger keeps the unflushed
        batch). A failure here must never escape the run loop's error handler,
        or ``_run`` dies without reconnecting or releasing the connector.
        """

        try:
            await self._log_event("runtime_error", payload)
        except Exception as exc:
            _logger().warning(
                "hosted_session_runtime_error_log_failed session_id=%s error=%s",
                self.definition.session_id,
                exc,
            )
