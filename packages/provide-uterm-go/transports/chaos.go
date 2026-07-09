//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"fmt"
	"math/rand"
	"sync"
	"time"
)

// ChaosConfig configures the fault injection of a ChaosTransport.
type ChaosConfig struct {
	// Seed seeds the deterministic RNG.
	Seed int64
	// DisconnectEveryNReceives injects a disconnect every N Receive calls (0 = off).
	DisconnectEveryNReceives int
	// TimeoutEveryNReceives returns empty bytes every N Receive calls (0 = off).
	TimeoutEveryNReceives int
	// MaxJitterMs adds up to this many ms of random delay per Receive (0 = off).
	MaxJitterMs int
	// Label prefixes injected error messages.
	Label string
}

// ChaosTransport wraps an inner transport and injects faults deterministically.
// It is a port of the Python ChaosTransport used for resilience testing.
type ChaosTransport struct {
	inner       ConnectionTransport
	rng         *rand.Rand
	disconnectN int
	timeoutN    int
	maxJitterMs int
	label       string

	mu      sync.Mutex
	rxCount int
}

// NewChaosTransport wraps inner with fault injection per cfg.
func NewChaosTransport(inner ConnectionTransport, cfg ChaosConfig) *ChaosTransport {
	label := cfg.Label
	if label == "" {
		label = "chaos"
	}
	seed := cfg.Seed
	if seed == 0 {
		seed = 1
	}
	return &ChaosTransport{
		inner:       inner,
		rng:         rand.New(rand.NewSource(seed)), //nolint:gosec // chaos testing, not crypto
		disconnectN: cfg.DisconnectEveryNReceives,
		timeoutN:    cfg.TimeoutEveryNReceives,
		maxJitterMs: cfg.MaxJitterMs,
		label:       label,
	}
}

// Connect delegates to the inner transport.
func (c *ChaosTransport) Connect(ctx context.Context, host string, port int, opts ConnectOptions) error {
	return c.inner.Connect(ctx, host, port, opts)
}

// Disconnect delegates to the inner transport.
func (c *ChaosTransport) Disconnect(ctx context.Context) error {
	return c.inner.Disconnect(ctx)
}

// Send delegates to the inner transport.
func (c *ChaosTransport) Send(ctx context.Context, data []byte) error {
	return c.inner.Send(ctx, data)
}

// Receive injects faults, then delegates to the inner transport.
func (c *ChaosTransport) Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error) {
	c.mu.Lock()
	c.rxCount++
	count := c.rxCount
	var jitter time.Duration
	if c.maxJitterMs > 0 {
		jitter = time.Duration(c.rng.Float64() * float64(c.maxJitterMs) * float64(time.Millisecond))
	}
	c.mu.Unlock()

	if jitter > 0 {
		if err := sleepCtx(ctx, jitter); err != nil {
			return nil, err
		}
	}

	if c.disconnectN > 0 && count%c.disconnectN == 0 {
		_ = c.inner.Disconnect(ctx)
		return nil, fmt.Errorf("%s: injected disconnect on receive #%d", c.label, count)
	}

	if c.timeoutN > 0 && count%c.timeoutN == 0 {
		if err := sleepCtx(ctx, timeout); err != nil {
			return nil, err
		}
		return []byte{}, nil
	}

	return c.inner.Receive(ctx, maxBytes, timeout)
}

// IsConnected delegates to the inner transport.
func (c *ChaosTransport) IsConnected() bool {
	return c.inner.IsConnected()
}

// sleepCtx sleeps for d unless ctx is cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Compile-time assertion that ChaosTransport implements ConnectionTransport.
var _ ConnectionTransport = (*ChaosTransport)(nil)
