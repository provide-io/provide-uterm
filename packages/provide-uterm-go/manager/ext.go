//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"time"
)

// Standardized DAS events for the agent lifecycle. The Python side builds these
// via provide.telemetry event(); here they are the equivalent dotted strings
// used as structured-log event names.
const (
	EventAgentSpawned = "terminal.agent.spawned"
	EventAgentExited  = "terminal.agent.exited"
	EventAgentKilled  = "terminal.agent.killed"
)

// AgentSpawnPolicyGate decides whether an agent spawn is allowed. Mirrors the
// AgentSpawnPolicyGate protocol in manager/ext.py.
type AgentSpawnPolicyGate interface {
	// InterceptSpawn returns true to allow the spawn, false to reject.
	InterceptSpawn(ctx context.Context, agentID, configPath string, rawConfig map[string]any) bool
}

// NoOpAgentSpawnPolicyGate allows all spawns (the default gate).
type NoOpAgentSpawnPolicyGate struct{}

// InterceptSpawn always allows.
func (NoOpAgentSpawnPolicyGate) InterceptSpawn(_ context.Context, _, _ string, _ map[string]any) bool {
	return true
}

// WebhookAgentSpawnPolicyGate delegates spawn decisions to an external webhook,
// mirroring WebhookAgentSpawnPolicyGate in manager/ext.py.
type WebhookAgentSpawnPolicyGate struct {
	URL     string
	Secret  string
	Timeout time.Duration
	client  *http.Client
}

// NewWebhookAgentSpawnPolicyGate constructs a webhook gate.
func NewWebhookAgentSpawnPolicyGate(url, secret string, timeoutS float64) *WebhookAgentSpawnPolicyGate {
	if timeoutS <= 0 {
		timeoutS = 2.0
	}
	return &WebhookAgentSpawnPolicyGate{
		URL:     url,
		Secret:  secret,
		Timeout: time.Duration(timeoutS * float64(time.Second)),
	}
}

// canonicalJSON serializes payload with sorted keys and no spaces, matching
// json.dumps(payload, separators=(",", ":"), sort_keys=True). Go's json.Marshal
// already emits map keys in sorted order with compact separators, so it is
// byte-identical for the string-keyed shapes used here.
func canonicalJSON(payload map[string]any) []byte {
	b, _ := json.Marshal(payload)
	return b
}

// InterceptSpawn posts the signed request to the webhook and returns the
// "allow" decision.
func (g *WebhookAgentSpawnPolicyGate) InterceptSpawn(ctx context.Context, agentID, configPath string, rawConfig map[string]any) bool {
	if rawConfig == nil {
		rawConfig = map[string]any{}
	}
	payload := map[string]any{
		"agent_id":    agentID,
		"config_path": configPath,
		"raw_config":  rawConfig,
	}
	body := canonicalJSON(payload)
	client := g.client
	if client == nil {
		client = &http.Client{Timeout: g.Timeout}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.URL, bytes.NewReader(body))
	if err != nil {
		return false
	}
	req.Header.Set("Content-Type", "application/json")
	if g.Secret != "" {
		mac := hmac.New(sha256.New, []byte(g.Secret))
		mac.Write(body)
		req.Header.Set("X-Signature", "sha256="+hex.EncodeToString(mac.Sum(nil)))
	}
	resp, err := client.Do(req)
	if err != nil {
		return false
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return false
	}
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return false
	}
	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return false
	}
	allow, _ := decoded["allow"].(bool)
	return allow
}
