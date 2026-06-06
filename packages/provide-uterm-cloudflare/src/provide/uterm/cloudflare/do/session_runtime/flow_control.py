#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#

"""Application-layer flow controller for the Durable Object terminal relay.

workerd exposes no ``WebSocket.bufferedAmount`` (cloudflare/workerd#988), so a DO
cannot measure its own outbound buffer. Backpressure is therefore driven by
application-layer ACKs: each browser periodically reports the cumulative number of
bytes it has consumed, and ``inflight = sent - acked``. When the worst-case
inflight across *ACK-capable* browsers exceeds the high-water mark, the controller
signals the producer to pause (XOFF); when it falls below the low-water mark it
signals resume (hysteresis avoids flapping).

A browser that has gone silent — no ACK within ``ack_grace_s`` — is excluded from
the decision, so a stuck or non-ACKing client can never pause the producer
forever (see the "silent-client trap" in ``docs/ard-cloudflare-backpressure.md``).

This is pure logic with no Cloudflare/Pyodide dependency; ``SessionRuntime`` wires
it into the broadcast and ACK-ingestion paths (Tier A).
"""

from __future__ import annotations

PAUSE = "pause"
RESUME = "resume"


class FlowController:
    """Tracks per-browser sent/acked bytes and decides producer pause/resume."""

    def __init__(self, *, high_water: int, low_water: int, ack_grace_s: float) -> None:
        self.high_water = high_water
        self.low_water = low_water
        self.ack_grace_s = ack_grace_s
        self._sent: dict[str, int] = {}
        self._acked: dict[str, int] = {}
        self._last_ack: dict[str, float] = {}
        self._paused = False
        # Tier B (per-viewer fairness): sticky per-browser congestion (set above
        # high_water, cleared below low_water) plus the set of browsers that just
        # transitioned back to clear and therefore need a resync snapshot.
        self._congested: dict[str, bool] = {}
        self._recovered: set[str] = set()

    @property
    def paused(self) -> bool:
        return self._paused

    def on_sent(self, ws_id: str, nbytes: int) -> None:
        """Record *nbytes* sent to *ws_id* (accumulates)."""
        self._sent[ws_id] = self._sent.get(ws_id, 0) + nbytes
        self._refresh_congestion(ws_id)

    def on_ack(self, ws_id: str, acked_bytes: int, now: float) -> None:
        """Record a cumulative-bytes ACK from *ws_id*; monotonic, so a stale or
        replayed lower value never rewinds the consumed count."""
        self._acked[ws_id] = max(self._acked.get(ws_id, 0), acked_bytes)
        self._last_ack[ws_id] = now
        self._refresh_congestion(ws_id)

    def forget(self, ws_id: str) -> None:
        """Drop all state for a disconnected browser."""
        self._sent.pop(ws_id, None)
        self._acked.pop(ws_id, None)
        self._last_ack.pop(ws_id, None)
        self._congested.pop(ws_id, None)
        self._recovered.discard(ws_id)

    def _inflight(self, ws_id: str) -> int:
        return self._sent.get(ws_id, 0) - self._acked.get(ws_id, 0)

    def max_inflight(self, now: float) -> int:
        """Largest ``sent - acked`` among browsers that ACKed within the grace window."""
        best = 0
        for ws_id, last in self._last_ack.items():
            if now - last > self.ack_grace_s:
                continue
            inflight = self._inflight(ws_id)
            if inflight > best:
                best = inflight
        return best

    def decide(self, now: float) -> str | None:
        """Return ``PAUSE``, ``RESUME``, or ``None`` for the current inflight + state."""
        inflight = self.max_inflight(now)
        if not self._paused and inflight > self.high_water:
            self._paused = True
            return PAUSE
        if self._paused and inflight < self.low_water:
            self._paused = False
            return RESUME
        return None

    # ------------------------------------------------------------------
    # Tier B — per-viewer fairness
    # ------------------------------------------------------------------

    def _refresh_congestion(self, ws_id: str) -> None:
        """Update sticky congestion for *ws_id* with high/low-water hysteresis.

        A browser becomes congested once its inflight exceeds the high-water mark
        and stays congested until it drains below the low-water mark, at which point
        it is recorded as recovered (needs a resync snapshot).
        """
        inflight = self._inflight(ws_id)
        if not self._congested.get(ws_id, False):
            if inflight > self.high_water:
                self._congested[ws_id] = True
        elif inflight < self.low_water:
            self._congested[ws_id] = False
            self._recovered.add(ws_id)

    def is_congested(self, ws_id: str) -> bool:
        """Whether droppable (term) frames to *ws_id* should be skipped (Tier B)."""
        return self._congested.get(ws_id, False)

    def all_active_congested(self, now: float) -> bool:
        """True when every ACK-capable browser is congested → the producer should pause.

        If even the fastest consumer cannot keep up there is no point producing. A
        session with no recent ACKers returns False (best-effort, never stalls the
        producer for a silent client).
        """
        active = [ws_id for ws_id, last in self._last_ack.items() if now - last <= self.ack_grace_s]
        return bool(active) and all(self._congested.get(ws_id, False) for ws_id in active)

    def take_recovered(self) -> set[str]:
        """Return and clear the set of browsers that just un-congested (need resync)."""
        out = self._recovered
        self._recovered = set()
        return out
