//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"
)

func isEgressBlocked(err error) bool {
	var e *EgressBlockedError
	return errors.As(err, &e)
}

// TestConnectorTargetInjectedResolver drives AssertConnectorTargetAllowed with a
// stub resolver, covering metadata (always), private (flagged), and public.
func TestConnectorTargetInjectedResolver(t *testing.T) {
	guard := NewEgressGuard(func(_ context.Context, host string) ([]string, error) {
		switch host {
		case "metadata.example":
			return []string{"169.254.169.254"}, nil
		case "internal.example":
			return []string{"10.1.2.3"}, nil
		case "linklocal.example":
			return []string{"169.254.1.1"}, nil
		case "public.example":
			return []string{"93.184.216.34"}, nil
		case "nat64.example":
			return []string{"64:ff9b::169.254.169.254"}, nil
		}
		return nil, errors.New("nxdomain")
	}, nil)
	ctx := context.Background()

	cases := []struct {
		host         string
		blockPrivate bool
		wantBlocked  bool
	}{
		{"metadata.example", false, true},  // metadata blocked even without block_private
		{"metadata.example", true, true},   //
		{"nat64.example", false, true},     // NAT64-wrapped metadata blocked
		{"internal.example", false, false}, // private allowed by default
		{"internal.example", true, true},   // private blocked when flagged
		{"linklocal.example", true, true},  //
		{"public.example", true, false},    // public always allowed
		{"public.example", false, false},   //
	}
	for _, c := range cases {
		err := guard.AssertConnectorTargetAllowed(ctx, c.host, c.blockPrivate)
		if isEgressBlocked(err) != c.wantBlocked {
			t.Errorf("%s blockPrivate=%v: blocked=%v want %v (err=%v)",
				c.host, c.blockPrivate, isEgressBlocked(err), c.wantBlocked, err)
		}
	}
}

// TestConnectorTargetResolveFailFailsClosed covers the NXDOMAIN and empty-result
// branches (both must fail closed with EgressBlockedError, not propagate).
func TestConnectorTargetResolveFailFailsClosed(t *testing.T) {
	ctx := context.Background()
	nx := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, errors.New("nxdomain")
	}, nil)
	if err := nx.AssertConnectorTargetAllowed(ctx, "bad.example", false); !isEgressBlocked(err) {
		t.Fatalf("nxdomain: want EgressBlockedError, got %v", err)
	}
	empty := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, nil
	}, nil)
	if err := empty.AssertConnectorTargetAllowed(ctx, "empty.example", false); !isEgressBlocked(err) {
		t.Fatalf("empty resolve: want EgressBlockedError, got %v", err)
	}
	if err := empty.AssertWebhookTargetAllowed(ctx, "https://empty.example/x"); !isEgressBlocked(err) {
		t.Fatalf("webhook empty resolve: want EgressBlockedError, got %v", err)
	}
}

// TestResolverTimeoutFailsClosed proves a resolver that never returns is bounded
// by the resolve timeout and fails closed rather than hanging.
func TestResolverTimeoutFailsClosed(t *testing.T) {
	guard := NewEgressGuard(func(ctx context.Context, _ string) ([]string, error) {
		<-ctx.Done() // block until the guard's timeout cancels us
		return nil, ctx.Err()
	}, nil)
	guard.timeout = 20 * time.Millisecond
	done := make(chan error, 1)
	go func() {
		done <- guard.AssertConnectorTargetAllowed(context.Background(), "slow.example", false)
	}()
	select {
	case err := <-done:
		if !isEgressBlocked(err) {
			t.Fatalf("timeout: want EgressBlockedError, got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("guard hung past the resolve timeout")
	}
}

// TestResolverTimeoutIgnoringContext proves the hard timeout fires even if the
// resolver ignores context cancellation (the goroutine-select guard).
func TestResolverTimeoutIgnoringContext(t *testing.T) {
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		<-release // ignores ctx entirely
		return []string{"8.8.8.8"}, nil
	}, nil)
	guard.timeout = 20 * time.Millisecond
	err := guard.AssertConnectorTargetAllowed(context.Background(), "stuck.example", false)
	if !isEgressBlocked(err) {
		t.Fatalf("want EgressBlockedError on hard timeout, got %v", err)
	}
}

// TestWebhookDNSCacheTTL verifies the injected-clock cache: a second lookup
// inside the TTL reuses the cached result (resolver not re-called); past the TTL
// the resolver fires again.
func TestWebhookDNSCacheTTL(t *testing.T) {
	var calls int
	now := 1000.0
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		calls++
		return []string{"93.184.216.34"}, nil
	}, func() float64 { return now })
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		if err := guard.AssertWebhookTargetAllowed(ctx, "https://host.example/x"); err != nil {
			t.Fatalf("unexpected block: %v", err)
		}
	}
	if calls != 1 {
		t.Fatalf("within TTL: resolver called %d times, want 1", calls)
	}
	now += egressDNSTTLS + 1 // advance past the TTL
	if err := guard.AssertWebhookTargetAllowed(ctx, "https://host.example/x"); err != nil {
		t.Fatalf("post-TTL block: %v", err)
	}
	if calls != 2 {
		t.Fatalf("post-TTL: resolver called %d times, want 2", calls)
	}
}

// TestWebhookDNSNameResolvesToMetadata proves a rebinding name that resolves to
// a metadata IP is blocked (the guard checks the resolved address).
func TestWebhookDNSNameResolvesToMetadata(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return []string{"169.254.169.254"}, nil
	}, nil)
	if err := guard.AssertWebhookTargetAllowed(context.Background(), "https://rebind.example/hook"); !isEgressBlocked(err) {
		t.Fatalf("want EgressBlockedError, got %v", err)
	}
}

// TestSessionEgressDerivation covers host derivation per connector type plus the
// validation branches, using literal IPs so no resolver is needed.
func TestSessionEgressDerivation(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, errors.New("no resolver expected")
	}, nil)
	ctx := context.Background()

	cases := []struct {
		name        string
		ctype       string
		cfg         map[string]any
		block       bool
		wantBlocked bool
	}{
		{"ssh metadata always", "ssh", map[string]any{"host": "169.254.169.254"}, false, true},
		{"ssh private allowed", "ssh", map[string]any{"host": "10.0.0.5"}, false, false},
		{"ssh private blocked", "ssh", map[string]any{"host": "10.0.0.5"}, true, true},
		{"ssh key rejected", "ssh", map[string]any{"host": "1.2.3.4", "client_key_path": "/k"}, false, true},
		{"telnet public", "telnet", map[string]any{"host": "93.184.216.34"}, true, false},
		{"ws missing url", "websocket", map[string]any{}, false, true},
		{"ws bad scheme", "websocket", map[string]any{"url": "http://1.2.3.4/"}, false, true},
		{"ws hostless", "websocket", map[string]any{"url": "ws://"}, false, true},
		{"ws metadata", "websocket", map[string]any{"url": "wss://169.254.169.254/x"}, false, true},
		{"ws public", "websocket", map[string]any{"url": "wss://93.184.216.34/x"}, true, false},
		{"shell unguarded", "shell", map[string]any{}, true, false},
		{"pty unguarded", "pty", map[string]any{"command": "/bin/bash"}, true, false},
	}
	for _, c := range cases {
		err := guard.AssertSessionEgressAllowed(ctx, c.ctype, c.cfg, c.block)
		if isEgressBlocked(err) != c.wantBlocked {
			t.Errorf("%s: blocked=%v want %v (err=%v)", c.name, isEgressBlocked(err), c.wantBlocked, err)
		}
	}
}

// TestSessionEgressNonStringHost covers the fmt.Sprintf host-coercion branch
// (a non-string, non-nil host in connector_config).
func TestSessionEgressNonStringHost(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, errors.New("should reach literal-IP path only")
	}, nil)
	// host given as an int-like value that stringifies to a metadata IP is not a
	// realistic literal, so use a stringifiable public IP wrapped as any.
	err := guard.AssertSessionEgressAllowed(context.Background(), "ssh",
		map[string]any{"host": anyHost("93.184.216.34")}, true)
	if err != nil {
		t.Fatalf("stringified public host should be allowed, got %v", err)
	}
}

// TestSessionEgressNonStringHostIsActuallyChecked proves the stringified value
// from the `connectorConfig["host"] != nil` branch is the value that gets
// guarded, not silently dropped: a non-string host that stringifies to a
// metadata address must be blocked. A test using only an allowed address (as
// TestSessionEgressNonStringHost does) cannot distinguish "the branch ran and
// derived targetHost" from "the branch was skipped and targetHost stayed empty
// (vacuously allowed)", since both produce err == nil.
func TestSessionEgressNonStringHostIsActuallyChecked(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, errors.New("should reach literal-IP path only")
	}, nil)
	err := guard.AssertSessionEgressAllowed(context.Background(), "ssh",
		map[string]any{"host": anyHost("169.254.169.254")}, false)
	if !isEgressBlocked(err) {
		t.Fatalf("a stringified metadata host must be blocked, got %v", err)
	}
}

type anyHost string

func (a anyHost) String() string { return string(a) }

// TestWebhookUnparseableURL covers hostFromURL's url.Parse error branch (an
// unmatched IPv6 bracket) → empty host → allowed.
func TestWebhookUnparseableURL(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		t.Fatal("resolver must not fire for an unparseable URL")
		return nil, nil
	}, nil)
	if err := guard.AssertWebhookTargetAllowed(context.Background(), "http://[::1"); err != nil {
		t.Fatalf("unparseable URL should be allowed (host empty), got %v", err)
	}
}

// TestAssertIPAllowedInvalid covers the non-parseable literal branch.
func TestAssertIPAllowedInvalid(t *testing.T) {
	err := AssertIPAllowed("not-an-ip", false)
	if err == nil || isEgressBlocked(err) {
		t.Fatalf("want plain error for bad literal, got %v", err)
	}
}

// TestConnectorTargetNonIPResolved covers the "resolved address is not a
// parseable IP" continue branch (defensive against a bad resolver).
func TestConnectorTargetNonIPResolved(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return []string{"not-an-ip", "8.8.8.8"}, nil
	}, nil)
	if err := guard.AssertConnectorTargetAllowed(context.Background(), "weird.example", true); err != nil {
		t.Fatalf("non-IP entries should be skipped, got %v", err)
	}
	wh := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return []string{"garbage"}, nil
	}, nil)
	if err := wh.AssertWebhookTargetAllowed(context.Background(), "https://weird.example/x"); err != nil {
		t.Fatalf("non-IP webhook entry should be skipped, got %v", err)
	}
}

// TestWebhookResolverError covers the resolveCached error branch (resolver
// returns an error, not an empty result) → fail closed.
func TestWebhookResolverError(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, errors.New("servfail")
	}, nil)
	if err := guard.AssertWebhookTargetAllowed(context.Background(), "https://broken.example/x"); !isEgressBlocked(err) {
		t.Fatalf("resolver error must fail closed, got %v", err)
	}
}

// TestEgressGuardDefaults constructs a guard with all defaults (nil resolver +
// nil clock) to exercise the default-injection branches.
func TestEgressGuardDefaults(t *testing.T) {
	g := NewEgressGuard(nil, nil)
	if g.resolver == nil || g.now == nil {
		t.Fatal("defaults not injected")
	}
	// A plain `> 0` check is satisfied even if the default clock's
	// UnixNano()/1e9 conversion were mutated to UnixNano()*1e9 — the result is
	// still positive, just off by 18 orders of magnitude. Bound it to a plausible
	// epoch-*seconds* range (year 2001 .. year 5138) so a mutated divisor fails
	// this assertion instead of merely surviving a non-negativity check.
	now := g.now()
	const minPlausibleEpochS = 1e9  // 2001-09-09
	const maxPlausibleEpochS = 1e11 // 5138-11-16
	if now < minPlausibleEpochS || now > maxPlausibleEpochS {
		t.Fatalf("default clock = %v, want a plausible epoch-seconds value in [%v, %v]",
			now, minPlausibleEpochS, maxPlausibleEpochS)
	}
}

// TestEgressResolveTimeoutConstant pins computeEgressResolveTimeout's value.
// The multiplication lives in a function (rather than a bare package-level
// const) specifically so this assertion has a mutant to kill — see the
// doc comment on egressResolveTimeout.
func TestEgressResolveTimeoutConstant(t *testing.T) {
	if got := computeEgressResolveTimeout(); got != 5*time.Second {
		t.Errorf("computeEgressResolveTimeout() = %v, want 5s", got)
	}
}

// TestResolveCachedTTLBoundary pins the exact TTL edge: a lookup exactly
// ttlS old is a cache MISS (re-resolves), not a hit. This distinguishes
// `(now-c.at) < g.ttlS` from a `<=` boundary mutant, which the earlier
// "well past the TTL" assertion in TestWebhookDNSCacheTTL does not.
func TestResolveCachedTTLBoundary(t *testing.T) {
	var calls int
	now := 1000.0
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		calls++
		return []string{"93.184.216.34"}, nil
	}, func() float64 { return now })
	ctx := context.Background()

	if err := guard.AssertWebhookTargetAllowed(ctx, "https://ttl.example/x"); err != nil {
		t.Fatalf("initial resolve: %v", err)
	}
	if calls != 1 {
		t.Fatalf("initial resolve: calls = %d, want 1", calls)
	}

	now += egressDNSTTLS // exactly at the boundary: (now-c.at) == ttlS
	if err := guard.AssertWebhookTargetAllowed(ctx, "https://ttl.example/x"); err != nil {
		t.Fatalf("at-boundary resolve: %v", err)
	}
	if calls != 2 {
		t.Fatalf("at exactly the TTL boundary: calls = %d, want 2 (a stale-by-exactly-ttlS "+
			"entry must be treated as expired, not reused)", calls)
	}
}

// TestSessionEgressWebsocketPlainSchemeAllowed pins that a valid ws:// URL
// (not wss://) is not refused by the scheme guard. Every other websocket test
// case uses wss:// or a scheme that is rejected regardless of which half of
// `parsed.Scheme != "ws" && parsed.Scheme != "wss"` a mutant flips, so this is
// the one case that actually needs the "ws" comparison to be correct.
func TestSessionEgressWebsocketPlainSchemeAllowed(t *testing.T) {
	guard := NewEgressGuard(fixedResolver("93.184.216.34"), nil)
	err := guard.AssertSessionEgressAllowed(context.Background(), "websocket",
		map[string]any{"url": "ws://public.example/x"}, true)
	if err != nil {
		t.Fatalf("a plain ws:// URL to a public host must be allowed, got %v", err)
	}
}

// TestDecodeEmbeddedIPv4 exercises every branch of the IPv6-wrapper decoder
// directly: 6to4, NAT64 well-known, the deprecated IPv4-compatible form (both
// the ::/::1 exclusion and an ordinary payload), and a plain address that
// carries nothing to decode.
func TestDecodeEmbeddedIPv4(t *testing.T) {
	cases := []struct {
		name string
		ip   net.IP
		want net.IP // nil means "decodeEmbeddedIPv4 must return nil"
	}{
		{"6to4", net.ParseIP("2002:c000:0204::"), net.IPv4(192, 0, 2, 4)},
		{"NAT64 well-known", net.ParseIP("64:ff9b::169.254.169.254"), net.IPv4(169, 254, 169, 254)},
		{"deprecated IPv4-compatible, ordinary payload", net.IP{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 5},
			net.IPv4(0, 0, 0, 5)},
		{"deprecated IPv4-compatible, unspecified (::)", net.IP{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0}, nil},
		{"deprecated IPv4-compatible, loopback (::1)", net.IP{0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1}, nil},
		{"plain global IPv6, nothing embedded", net.ParseIP("2606:4700:4700::1111"), nil},
		{"malformed IP (wrong byte length)", net.IP{1, 2, 3}, nil},
	}
	for _, c := range cases {
		got := decodeEmbeddedIPv4(c.ip)
		switch {
		case c.want == nil && got != nil:
			t.Errorf("%s: decodeEmbeddedIPv4(%v) = %v, want nil", c.name, c.ip, got)
		case c.want != nil && !c.want.Equal(got):
			t.Errorf("%s: decodeEmbeddedIPv4(%v) = %v, want %v", c.name, c.ip, got, c.want)
		}
	}
}

// TestWebhookHostlessAllowed covers the empty-host early return.
func TestWebhookHostlessAllowed(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		t.Fatal("resolver should not fire for a hostless URL")
		return nil, nil
	}, nil)
	if err := guard.AssertWebhookTargetAllowed(context.Background(), "not a url"); err != nil {
		t.Fatalf("hostless URL should be allowed, got %v", err)
	}
}
