//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"time"
)

// NodeStatus is the discovery heartbeat payload. Port of discovery.NodeStatus.
type NodeStatus struct {
	NodeID         string  `json:"node_id"`
	ActiveSessions int     `json:"active_sessions"`
	WorkerCount    int     `json:"worker_count"`
	Timestamp      float64 `json:"timestamp"`
}

// DiscoveryProvider announces this node's status to a discovery service. Port
// of discovery.DiscoveryProvider.
type DiscoveryProvider interface {
	Announce(ctx context.Context, status NodeStatus) error
}

// NoOpDiscoveryProvider discards announcements. Port of NoOpDiscoveryProvider.
type NoOpDiscoveryProvider struct{}

// Announce is a no-op.
func (NoOpDiscoveryProvider) Announce(context.Context, NodeStatus) error { return nil }

// WebhookDiscoveryProvider POSTs NodeStatus JSON to a configured webhook. Port
// of discovery.WebhookDiscoveryProvider. The outbound URL is egress-guarded
// (SSRF): a blocked target is a best-effort no-op (no POST), like the Python
// try/except that swallows EgressBlockedError.
type WebhookDiscoveryProvider struct {
	url    string
	secret string
	client *http.Client
	guard  *EgressGuard
}

// NewWebhookDiscoveryProvider builds a provider. A nil guard uses a default one;
// timeoutS ≤ 0 uses 5s (the Python default).
func NewWebhookDiscoveryProvider(url, secret string, timeoutS float64, guard *EgressGuard) *WebhookDiscoveryProvider {
	if guard == nil {
		guard = NewEgressGuard(nil, nil)
	}
	if timeoutS <= 0 {
		timeoutS = 5.0
	}
	return &WebhookDiscoveryProvider{
		url:    url,
		secret: secret,
		client: &http.Client{Timeout: time.Duration(timeoutS * float64(time.Second))},
		guard:  guard,
	}
}

// Announce egress-guards the target then POSTs the status. Errors are returned
// for observability but the heartbeat loop treats them as best-effort.
func (p *WebhookDiscoveryProvider) Announce(ctx context.Context, status NodeStatus) error {
	if err := p.guard.AssertWebhookTargetAllowed(ctx, p.url); err != nil {
		return err
	}
	body, err := json.Marshal(status)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if p.secret != "" {
		req.Header.Set("Authorization", "Bearer "+p.secret)
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return err
	}
	return resp.Body.Close()
}

// buildDiscoveryProvider selects the provider from governance config, mirroring
// factory_sweeps.node_registry_heartbeat: a webhook provider when configured,
// else the no-op provider.
func (s *Server) buildDiscoveryProvider() DiscoveryProvider {
	g := s.cfg.Governance
	if g.DiscoveryProvider == "webhook" && g.RegistryWebhookURL != nil && *g.RegistryWebhookURL != "" {
		secret := ""
		if g.RegistryWebhookSecret != nil { // pragma: allowlist secret
			secret = *g.RegistryWebhookSecret
		}
		return NewWebhookDiscoveryProvider(*g.RegistryWebhookURL, secret, g.PolicyWebhookTimeoutS, s.egress)
	}
	return NoOpDiscoveryProvider{}
}

// nodeStatus snapshots this node's status for a heartbeat. Port of the
// NodeStatus construction in node_registry_heartbeat.
func (s *Server) nodeStatus(ctx context.Context) NodeStatus {
	return NodeStatus{
		NodeID:         s.cfg.Server.NodeID,
		ActiveSessions: s.deps.Hub.BrowserCountTotal(ctx),
		WorkerCount:    s.deps.Hub.Registry.Len(),
		Timestamp:      s.clock.Wall(),
	}
}
