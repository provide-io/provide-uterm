//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"time"
)

// Sweep intervals, matching factory_sweeps.py.
const (
	approvalSweepInterval     = 30 * time.Second
	idleSweepInterval         = 60 * time.Second
	sessionSweepInterval      = 300 * time.Second
	tunnelInviteSweepInterval = 60 * time.Second
)

// StartSweeps launches the background maintenance goroutines. They run until ctx
// is cancelled; Shutdown waits for them via sweepWG. Idempotent.
//
// Deviation: the recording-retention and control-plane-reap sweeps are not
// launched here — they depend on infrastructure (recording directory,
// control-plane engine) outside this HTTP layer's dependency set. The
// node-registry heartbeat IS launched (its inputs — governance config, hub
// counts, egress guard — are all available to the server).
func (s *Server) StartSweeps(ctx context.Context) {
	s.sweepOnce.Do(func() {
		s.launchSweep(ctx, approvalSweepInterval, s.sweepApprovals)
		s.launchSweep(ctx, idleSweepInterval, s.sweepIdleSessions)
		s.launchSweep(ctx, sessionSweepInterval, s.sweepExpiredSessions)
		s.launchSweep(ctx, tunnelInviteSweepInterval, s.sweepTunnelInvites)
		s.startNodeRegistryHeartbeat(ctx)
	})
}

// startNodeRegistryHeartbeat launches the discovery announcer on the governance-
// configured cadence, but only when a webhook provider is actually configured
// (matching factory_sweeps.node_registry_heartbeat, which returns early for the
// no-op provider). Errors are best-effort — logged and swallowed.
func (s *Server) startNodeRegistryHeartbeat(ctx context.Context) {
	provider := s.buildDiscoveryProvider()
	if _, noop := provider.(NoOpDiscoveryProvider); noop {
		return
	}
	interval := time.Duration(s.cfg.Governance.RegistryWebhookIntervalS * float64(time.Second))
	if interval <= 0 {
		interval = 60 * time.Second
	}
	s.launchSweep(ctx, interval, func(ctx context.Context) {
		if err := provider.Announce(ctx, s.nodeStatus(ctx)); err != nil {
			s.logger.Warn("node_registry_heartbeat_failed", "error", err)
		}
	})
}

// sweepTunnelInvites drops expired one-time tunnel invites. Port of
// sweep_expired_tunnel_invites.
func (s *Server) sweepTunnelInvites(context.Context) {
	s.deps.TunnelStore.SweepExpired(s.clock.Wall())
}

// launchSweep runs fn on a fixed cadence until ctx is done.
func (s *Server) launchSweep(ctx context.Context, interval time.Duration, fn func(context.Context)) {
	s.sweepWG.Add(1)
	go func() {
		defer s.sweepWG.Done()
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				fn(ctx)
			}
		}
	}()
}

// sweepApprovals times out pending approvals. Port of sweep_expired_approvals.
func (s *Server) sweepApprovals(context.Context) {
	s.deps.Hub.Approvals.CleanupExpired()
}

// sweepIdleSessions disconnects idle workers. Port of sweep_idle_sessions.
func (s *Server) sweepIdleSessions(ctx context.Context) {
	if s.cfg.SessionIdleTimeoutS <= 0 {
		return
	}
	for _, cand := range s.deps.Hub.GetIdleCandidates(ctx, float64(s.cfg.SessionIdleTimeoutS)) {
		_, _ = s.deps.Hub.DisconnectWorker(ctx, cand.WorkerID)
	}
}

// sweepExpiredSessions deletes stopped sessions past their retention window.
// Port of sweep_expired_sessions.
func (s *Server) sweepExpiredSessions(ctx context.Context) {
	if s.cfg.SessionRetentionS <= 0 {
		return
	}
	retention := float64(s.cfg.SessionRetentionS)
	now := s.clock.Wall()
	for _, it := range s.deps.Registry.ListWithDefinitions(ctx) {
		st := it.Status
		if st == nil || st.LifecycleState != "stopped" || st.StoppedAt == nil {
			continue
		}
		if now-*st.StoppedAt >= retention {
			_ = s.deps.Registry.DeleteSession(ctx, st.SessionID)
		}
	}
}
