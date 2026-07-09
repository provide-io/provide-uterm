//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
)

// ErrRetriesExhausted is returned when the reconnect budget is exhausted.
var ErrRetriesExhausted = errors.New("reconnect retries exhausted")

// ReconnectPolicy holds the retry budget and backoff for reconnect attempts.
// It is the Go port of the Python ReconnectPolicy dataclass.
type ReconnectPolicy struct {
	// MaxRetries is the number of reconnect attempts before giving up.
	MaxRetries int
	// BaseBackoff is the initial backoff delay.
	BaseBackoff time.Duration
	// MaxBackoff is the ceiling for the backoff delay.
	MaxBackoff time.Duration
}

// DefaultReconnectPolicy returns the policy seeded from the defaults package
// (RECONNECT_* constants), mirroring TerminalDefaults.
func DefaultReconnectPolicy() ReconnectPolicy {
	return ReconnectPolicy{
		MaxRetries:  defaults.ReconnectMaxRetries,
		BaseBackoff: secondsToDuration(defaults.ReconnectBaseBackoffS),
		MaxBackoff:  secondsToDuration(defaults.ReconnectMaxBackoffS),
	}
}

func secondsToDuration(s float64) time.Duration {
	return time.Duration(s * float64(time.Second))
}

// policyDelay computes bounded exponential backoff for a one-based attempt
// number. Direct port of Python's _policy_delay.
func (p ReconnectPolicy) policyDelay(attempt int) time.Duration {
	power := attempt - 1
	if power < 0 {
		power = 0
	}
	delay := p.BaseBackoff * (1 << power)
	if delay > p.MaxBackoff {
		delay = p.MaxBackoff
	}
	return delay
}

// TransportFactory creates a fresh, unconnected inner transport.
type TransportFactory func() ConnectionTransport

// SleepFunc sleeps for d unless ctx is cancelled. Injectable so tests can avoid
// real sleeps.
type SleepFunc func(ctx context.Context, d time.Duration) error

// IsRetryableFunc reports whether an error should trigger a reconnect.
type IsRetryableFunc func(error) bool

// defaultIsRetryable mirrors the Python retryable set (ConnectionError, OSError,
// websockets ConnectionClosed): transport-layer failures reconnect, logic
// errors do not.
func defaultIsRetryable(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, ErrConnectionClosed) || errors.Is(err, ErrNotConnected) || errors.Is(err, io.EOF) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr)
}

// ReconnectingTransport wraps a ConnectionTransport with automatic reconnection
// on transport drops. It is the Go port of the Python ReconnectingSession, and
// itself implements ConnectionTransport so it is drop-in interchangeable.
type ReconnectingTransport struct {
	factory     TransportFactory
	policy      ReconnectPolicy
	sleep       SleepFunc
	isRetryable IsRetryableFunc
	onReconnect func(ConnectionTransport)

	mu    sync.Mutex
	inner ConnectionTransport
	host  string
	port  int
	opts  ConnectOptions
}

// ReconnectingOptions configures a ReconnectingTransport.
type ReconnectingOptions struct {
	// Policy is the retry budget/backoff. Zero value uses DefaultReconnectPolicy.
	Policy *ReconnectPolicy
	// Sleep is the (injectable) backoff sleeper. Nil uses a real timer.
	Sleep SleepFunc
	// IsRetryable classifies errors. Nil uses defaultIsRetryable.
	IsRetryable IsRetryableFunc
	// OnReconnect is invoked with the freshly-connected inner transport after a
	// successful reconnect.
	OnReconnect func(ConnectionTransport)
}

// NewReconnectingTransport wraps a transport factory with reconnection logic.
func NewReconnectingTransport(factory TransportFactory, opts ReconnectingOptions) *ReconnectingTransport {
	policy := DefaultReconnectPolicy()
	if opts.Policy != nil {
		policy = *opts.Policy
	}
	sleep := opts.Sleep
	if sleep == nil {
		sleep = sleepCtx
	}
	isRetryable := opts.IsRetryable
	if isRetryable == nil {
		isRetryable = defaultIsRetryable
	}
	return &ReconnectingTransport{
		factory:     factory,
		policy:      policy,
		sleep:       sleep,
		isRetryable: isRetryable,
		onReconnect: opts.OnReconnect,
	}
}

// Connect establishes the first connection (with a retry budget) and stores the
// target for later reconnects.
func (r *ReconnectingTransport) Connect(ctx context.Context, host string, port int, opts ConnectOptions) error {
	r.mu.Lock()
	r.host = host
	r.port = port
	r.opts = opts
	r.mu.Unlock()

	inner, err := r.connectWithRetries(ctx)
	if err != nil {
		return err
	}
	r.mu.Lock()
	r.inner = inner
	r.mu.Unlock()
	return nil
}

// connectWithRetries dials via the factory with an exponential backoff budget.
// Direct port of Python's connect_with_retries.
func (r *ReconnectingTransport) connectWithRetries(ctx context.Context) (ConnectionTransport, error) {
	r.mu.Lock()
	host, port, opts := r.host, r.port, r.opts
	r.mu.Unlock()

	logger := ptel.GetLogger(ctx, "provide.uterm.transports.reconnect")
	retries := 0
	for {
		inner := r.factory()
		err := inner.Connect(ctx, host, port, opts)
		if err == nil {
			return inner, nil
		}
		if retries >= r.policy.MaxRetries {
			return nil, fmt.Errorf("%w: %v", ErrRetriesExhausted, err)
		}
		retries++
		logger.Debug("reconnect attempt", "attempt", retries, "err", err.Error())
		if serr := r.sleep(ctx, r.policy.policyDelay(retries)); serr != nil {
			return nil, serr
		}
	}
}

// reconnect closes the current inner transport, backs off, and reconnects.
// Direct port of Python's _reconnect.
func (r *ReconnectingTransport) reconnect(ctx context.Context, attempt int) error {
	r.mu.Lock()
	old := r.inner
	r.mu.Unlock()
	if old != nil {
		_ = old.Disconnect(ctx)
	}

	if r.policy.BaseBackoff > 0 {
		delay := r.policy.policyDelay(attempt)
		if delay > 0 {
			if err := r.sleep(ctx, delay); err != nil {
				return err
			}
		}
	}

	inner, err := r.connectWithRetries(ctx)
	if err != nil {
		return err
	}
	r.mu.Lock()
	r.inner = inner
	r.mu.Unlock()
	if r.onReconnect != nil {
		r.onReconnect(inner)
	}
	return nil
}

// runWithReconnect runs op, reconnecting on retryable failures. Direct port of
// Python's _run_with_reconnect.
func (r *ReconnectingTransport) runWithReconnect(ctx context.Context, op func(ConnectionTransport) error) error {
	retries := 0
	for {
		r.mu.Lock()
		inner := r.inner
		r.mu.Unlock()
		if inner == nil {
			return fmt.Errorf("%w: reconnecting transport not connected", ErrNotConnected)
		}
		err := op(inner)
		if err == nil {
			return nil
		}
		if !r.isRetryable(err) {
			return err
		}
		if retries >= r.policy.MaxRetries {
			_ = inner.Disconnect(ctx)
			return fmt.Errorf("%w: %v", ErrRetriesExhausted, err)
		}
		retries++
		if rerr := r.reconnect(ctx, retries); rerr != nil {
			return rerr
		}
	}
}

// Disconnect closes the active inner transport.
func (r *ReconnectingTransport) Disconnect(ctx context.Context) error {
	r.mu.Lock()
	inner := r.inner
	r.inner = nil
	r.mu.Unlock()
	if inner == nil {
		return nil
	}
	return inner.Disconnect(ctx)
}

// Send transmits data, reconnecting on retryable failures.
func (r *ReconnectingTransport) Send(ctx context.Context, data []byte) error {
	return r.runWithReconnect(ctx, func(t ConnectionTransport) error {
		return t.Send(ctx, data)
	})
}

// Receive reads bytes, reconnecting on retryable failures. Note: a receive
// timeout returns an empty slice with a nil error and is not treated as a drop.
func (r *ReconnectingTransport) Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error) {
	var out []byte
	err := r.runWithReconnect(ctx, func(t ConnectionTransport) error {
		var innerErr error
		out, innerErr = t.Receive(ctx, maxBytes, timeout)
		return innerErr
	})
	return out, err
}

// IsConnected reports whether the active inner transport is connected.
func (r *ReconnectingTransport) IsConnected() bool {
	r.mu.Lock()
	inner := r.inner
	r.mu.Unlock()
	return inner != nil && inner.IsConnected()
}

// Inner returns the currently-active inner transport (may be nil).
func (r *ReconnectingTransport) Inner() ConnectionTransport {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.inner
}

// Compile-time assertion that ReconnectingTransport implements ConnectionTransport.
var _ ConnectionTransport = (*ReconnectingTransport)(nil)
