#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Plugin boundary protocols for the generic swarm manager.

Games implement these protocols (duck typing) and pass instances to
``create_manager_app()`` to inject game-specific behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from provide.uterm.manager.models import AgentStatusBase


@runtime_checkable
class AccountPoolPlugin(Protocol):
    """Manages a cross-process account pool with lease/cooldown semantics."""

    def release_by_agent(self, *, agent_id: str, cooldown_s: int = 0) -> bool:
        """Release the account leased by *agent_id*; return ``True`` if released."""
        ...

    def mark_account_bust(self, *, agent_id: str) -> None:
        """Mark the account held by the agent as busted."""
        ...

    def summary(self) -> dict[str, Any]:
        """Return counts: accounts_total, leased, cooldown, available."""
        ...

    def list_accounts_safe(self) -> list[dict[str, Any]]:
        """Return accounts with credentials redacted."""
        ...


@runtime_checkable
class IdentityStorePlugin(Protocol):
    """Persistent per-agent credentials and session history."""

    def load(self, agent_id: str) -> Any | None:
        """Load an agent record by ID (or ``None`` if absent)."""
        ...

    def list_records(self) -> list[Any]:
        """Return all identity records."""
        ...


@runtime_checkable
class ManagedAgentPlugin(Protocol):
    """Local in-process agent resolution and command dispatch."""

    def resolve_local_agent(self, agent_status: AgentStatusBase) -> tuple[Any | None, str | None]:
        """Return ``(agent_instance, session_id)`` or ``(None, None)``."""
        ...

    async def dispatch_command(self, agent: Any, command: str, **kwargs: Any) -> dict[str, Any]:
        """Execute a manager command on a local agent instance."""
        ...

    def build_details(self, agent_status: AgentStatusBase, agent: Any | None, session_id: str | None) -> dict[str, Any]:
        """Build a unified managed-agent details view."""
        ...

    def build_action_response(
        self,
        agent_id: str,
        action: str,
        source: str,
        *,
        applied: bool,
        queued: bool,
        result: dict[str, Any],
        state: str,
    ) -> dict[str, Any]:
        """Normalise a control action result."""
        ...

    def describe_runtime(self, agent: Any | None, session_id: str | None) -> dict[str, Any] | None:
        """Return local runtime capabilities, or None."""
        ...


@runtime_checkable
class StatusUpdatePlugin(Protocol):
    """Merges game-specific fields from a worker status report."""

    def apply_update(self, agent: AgentStatusBase, payload: dict[str, Any], manager: Any) -> None:
        """Apply game-specific fields from *payload* onto *agent*."""
        ...


@runtime_checkable
class TimeseriesPlugin(Protocol):
    """Custom timeseries row building and summary enrichment."""

    def build_row(self, status: Any, reason: str) -> dict[str, Any]:
        """Build one timeseries sample row from the current swarm status."""
        ...

    def get_summary(self, timeseries_mgr: Any, window_minutes: int) -> dict[str, Any]:
        """Build a trailing-window summary from timeseries data."""
        ...


@runtime_checkable
class WorkerRegistryPlugin(Protocol):
    """Maps a worker type to its worker subprocess module."""

    @property
    def worker_type(self) -> str:
        """The worker identifier, e.g. ``"tradewars"``."""
        ...

    @property
    def worker_module(self) -> str:
        """Fully-qualified Python module path for the worker process."""
        ...

    def configure_worker_env(
        self,
        env: dict[str, str],
        agent_status: AgentStatusBase,
        manager: Any,
        *,
        raw_config: dict[str, Any] | None = None,
    ) -> None:
        """Inject game-specific environment variables before spawning.

        *raw_config* is the raw YAML dict for the agent's config file, allowing
        plugins to read game-specific keys without the generic manager needing
        to know about them.
        """
        ...
