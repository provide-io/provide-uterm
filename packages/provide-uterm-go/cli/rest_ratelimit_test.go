//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// buildForRateLimits assembles a server from a default config with the two
// REST rate keys optionally overridden, and returns the hub the routes will
// consult. Proves the config value reaches the limiter rather than stopping at
// the config struct.
func buildForRateLimits(t *testing.T, mutate func(*serverconfig.UtermServerConfig)) *serverBundle {
	t.Helper()
	cfg := serverconfig.DefaultServerConfig()
	cfg.Sessions = nil // no auto-start sessions; nothing is served here
	if mutate != nil {
		mutate(cfg)
	}
	bundle, err := buildServerFromConfig(context.Background(), cfg, "")
	if err != nil {
		t.Fatalf("buildServerFromConfig: %v", err)
	}
	t.Cleanup(func() { _ = bundle.engine.Close(context.Background()) })
	return bundle
}

// TestRestRateLimitsDefaultToHubRates pins that a deployment setting neither
// key gets the same limiter it gets today: 5/s acquire, 20/s send.
func TestRestRateLimitsDefaultToHubRates(t *testing.T) {
	b := buildForRateLimits(t, nil)
	if got := b.hub.Limiter.AcquireRate(); got != 5 {
		t.Errorf("default acquire rate = %v, want 5", got)
	}
	if got := b.hub.Limiter.SendRate(); got != 20 {
		t.Errorf("default send rate = %v, want 20", got)
	}
}

// TestRestRateLimitsThreadFromConfig pins that each key independently changes
// the limiter the REST hijack routes consult.
func TestRestRateLimitsThreadFromConfig(t *testing.T) {
	b := buildForRateLimits(t, func(c *serverconfig.UtermServerConfig) {
		c.RestAcquireRateLimitPerSec = 1.5
		c.RestSendRateLimitPerSec = 3
	})
	if got := b.hub.Limiter.AcquireRate(); got != 1.5 {
		t.Errorf("acquire rate = %v, want 1.5", got)
	}
	if got := b.hub.Limiter.SendRate(); got != 3 {
		t.Errorf("send rate = %v, want 3", got)
	}
}

// TestRestRateLimitsGovernAdmission pins the configured rates as the actual
// budgets, not just recorded numbers. Burst is one second of the same rate, so
// a 1/s acquire budget admits one call and a 2/s send budget admits two before
// the wall clock has had time to refill either.
func TestRestRateLimitsGovernAdmission(t *testing.T) {
	b := buildForRateLimits(t, func(c *serverconfig.UtermServerConfig) {
		c.RestAcquireRateLimitPerSec = 1
		c.RestSendRateLimitPerSec = 2
	})
	if !b.hub.Limiter.AllowRESTAcquire("client-a") {
		t.Fatal("first acquire should be admitted")
	}
	if b.hub.Limiter.AllowRESTAcquire("client-a") {
		t.Error("second acquire against a 1/s budget should be refused")
	}
	for i := 0; i < 2; i++ {
		if !b.hub.Limiter.AllowRESTSend("client-a") {
			t.Fatalf("send %d should be admitted by a 2/s budget", i)
		}
	}
	if b.hub.Limiter.AllowRESTSend("client-a") {
		t.Error("third send against a 2/s budget should be refused")
	}
}
