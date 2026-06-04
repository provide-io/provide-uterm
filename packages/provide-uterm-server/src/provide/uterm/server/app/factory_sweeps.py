#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Background sweep / heartbeat coroutines for the hosted terminal server.

These are the periodic maintenance loops scheduled by the application
lifespan: idle-session disconnects, retention sweeps, tunnel-token expiry,
approval timeouts, control-plane reaping, audit-head checkpointing, and the
node-registry heartbeat.  Each is a plain module-level coroutine taking its
dependencies as keyword args so ``factory_impl`` can schedule them with
``asyncio.create_task(...)``.

They sleep via the module-global ``asyncio.sleep`` exactly as before; tests
patch ``asyncio.sleep`` on the shared module object, so the fast-sleep harness
keeps working regardless of which module the loop body lives in.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import TYPE_CHECKING

from provide.telemetry import get_logger
from provide.uterm.server.tunnel_invites import sweep_expired_tunnel_invites

if TYPE_CHECKING:
    from provide.uterm.control.plane import ControlPlane as SharedControlPlane
    from provide.uterm.server.audit_chain import AuditChain
    from provide.uterm.server.bridge.hub import TermHub
    from provide.uterm.server.models import ServerConfig
    from provide.uterm.server.registry import SessionRegistry

logger = get_logger(__name__)

# Cadence for the approval-expiry sweep: time out expired PENDING approvals
# (firing on_expired so hold buffers drain) and prune old terminal entries.
# Matches the tunnel-token sweep cadence — both reap short-lived hold state.
_APPROVAL_SWEEP_INTERVAL_S = 30

# Environment variables that almost-always indicate a multi-replica
# orchestrator. Their presence alone doesn't *prove* multi-replica
# operation (a single-replica k8s deployment also sets KUBERNETES_*),
# but it raises the question loudly enough that the startup banner
# should escalate when control-plane durability is process-local.
_MULTI_REPLICA_ENV_HINTS: tuple[tuple[str, str], ...] = (
    ("KUBERNETES_SERVICE_HOST", "Kubernetes"),
    ("K_SERVICE", "Cloud Run"),
    ("WEBSITE_INSTANCE_ID", "Azure App Service"),
    ("ECS_CONTAINER_METADATA_URI", "AWS ECS"),
    ("ECS_CONTAINER_METADATA_URI_V4", "AWS ECS"),
    ("FLY_APP_NAME", "Fly.io"),
)


async def cancel_and_drain(*tasks: asyncio.Task[None]) -> None:
    """Cancel every task, then await each one, suppressing ``CancelledError``.

    Used by the application lifespan teardown to stop all background sweep /
    heartbeat tasks; cancelling them all up front lets the awaits proceed
    without serializing one shutdown-grace-period behind the next.
    """
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _detect_multi_replica_environment() -> set[str]:
    """Return a set of orchestrator names detected from process env.

    Returns an empty set in environments that look single-replica
    (bare-metal, single docker container, single VM, dev workstation).
    """
    found: set[str] = set()
    for env_var, label in _MULTI_REPLICA_ENV_HINTS:
        if os.environ.get(env_var):
            found.add(label)
    return found


async def sweep_idle_sessions(*, config: ServerConfig, hub: TermHub) -> None:
    """Periodically disconnect sessions with no activity beyond the configured timeout."""
    timeout_s = config.session_idle_timeout_s
    while True:
        await asyncio.sleep(60)
        if timeout_s <= 0:
            continue
        now = time.time()
        candidates = await hub.get_idle_candidates(timeout_s)
        for worker_id, last_at in candidates:
            try:
                logger.info(
                    "session_idle_timeout worker_id=%s idle_s=%d",
                    worker_id,
                    int(now - last_at),
                )
                await hub.disconnect_worker(worker_id)
            except Exception:
                logger.exception("session_idle_timeout_error worker_id=%s", worker_id)


async def sweep_expired_sessions(*, config: ServerConfig, registry: SessionRegistry) -> None:
    """Remove stopped sessions older than session_retention_s."""
    retention_s = config.session_retention_s
    while True:
        await asyncio.sleep(300)
        if retention_s <= 0:
            continue
        now = time.time()
        pairs = await registry.list_sessions_with_definitions()
        for sess_status, _definition in pairs:
            if sess_status.lifecycle_state != "stopped":
                continue
            if sess_status.stopped_at is None:
                continue
            if (now - sess_status.stopped_at) >= retention_s:
                try:
                    await registry.delete_session(sess_status.session_id)
                    logger.info(
                        "session_retention_sweep session_id=%s age_s=%d",
                        sess_status.session_id,
                        int(now - sess_status.stopped_at),
                    )
                except Exception:
                    logger.exception("session_retention_sweep_error session_id=%s", sess_status.session_id)


async def sweep_expired_recordings(*, config: ServerConfig) -> None:
    """Delete local recording files older than recording.retention_s."""
    retention_s = config.recording.retention_s
    if retention_s <= 0 or config.recording.store_type != "local":
        while True:
            await asyncio.sleep(300)
        # unreachable; keeps cancellation/loop behavior identical
    directory = config.recording.directory
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for path in directory.glob("*.jsonl"):
            try:
                age_s = now - path.stat().st_mtime
                if age_s >= retention_s:
                    path.unlink(missing_ok=True)
                    logger.info(
                        "recording_retention_sweep path=%s age_s=%d",
                        str(path),
                        int(age_s),
                    )
            except Exception:
                logger.exception("recording_retention_sweep_error path=%s", str(path))


async def sweep_expired_tunnel_tokens(
    *,
    tunnel_tokens: dict[str, dict[str, object]],
    tunnel_invites: dict[str, dict[str, object]],
) -> None:
    """Periodically remove expired tunnel tokens and pending invites."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        sweep_expired_tunnel_invites(tunnel_invites, now=now)
        expired: list[str] = []
        for sid, state in tunnel_tokens.items():
            expires_at = state.get("expires_at")
            if isinstance(expires_at, (int, float)) and now > float(expires_at):
                expired.append(sid)
        for sid in expired:
            tunnel_tokens.pop(sid, None)
            logger.info("tunnel_token_expired session_id=%s swept=true", sid)


async def sweep_expired_approvals(*, hub: TermHub) -> None:
    """Periodically time out expired pending approvals and prune old ones."""
    while True:
        await asyncio.sleep(_APPROVAL_SWEEP_INTERVAL_S)
        try:
            await hub.approval_store.cleanup_expired()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("approval_sweep_error")


async def sweep_control_plane_reap(*, config: ServerConfig, control_plane: SharedControlPlane) -> None:
    """Periodically physically-delete expired/soft-deleted control-plane rows."""
    while True:
        await asyncio.sleep(config.control_plane.reap_interval_s)
        try:
            deleted = await control_plane.reap(now=time.time(), retention_s=config.control_plane.reap_retention_s)
            if deleted:
                logger.info("control_plane_reap deleted_rows=%d", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("control_plane_reap_error")


async def checkpoint_audit_head(
    chain: AuditChain,
    *,
    config: ServerConfig,
    control_plane: SharedControlPlane,
) -> None:
    """Periodically checkpoint the audit-chain head into the control plane.

    Cheap atomic reads of ``chain.seq``/``chain.last_hash`` (no awaits in the
    synchronous append path) flushed to the durable control-plane head, which
    is the cross-restart anti-rollback anchor. ``set_audit_head`` is monotonic
    so a checkpoint can never move the head backwards.
    """
    while True:
        await asyncio.sleep(config.control_plane.reap_interval_s)
        try:
            await control_plane.set_audit_head(chain.seq, chain.last_hash)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audit_head_checkpoint_error")


async def node_registry_heartbeat(*, config: ServerConfig, hub: TermHub) -> None:
    """Periodically announce Node status to the External Management Tier."""
    from provide.uterm.server.discovery import (
        DiscoveryProvider,
        NodeStatus,
        NoOpDiscoveryProvider,
        WebhookDiscoveryProvider,
    )

    discovery_provider: DiscoveryProvider
    if config.governance.discovery_provider == "webhook" and config.governance.registry_webhook_url:
        discovery_provider = WebhookDiscoveryProvider(
            url=config.governance.registry_webhook_url,
            secret=config.governance.registry_webhook_secret,
        )
    else:
        discovery_provider = NoOpDiscoveryProvider()

    if isinstance(discovery_provider, NoOpDiscoveryProvider):
        return

    interval = config.governance.registry_webhook_interval_s
    node_id = getattr(config.server, "node_id", "default")

    while True:
        try:
            node_status = NodeStatus(
                node_id=node_id,
                active_sessions=await hub.browser_count_total(),
                worker_count=len(hub.registry._workers),
                timestamp=time.time(),
            )
            await discovery_provider.announce(node_status)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("node_registry_heartbeat_failed")
        await asyncio.sleep(interval)
