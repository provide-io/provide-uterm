//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net"
	"testing"
)

// TestWebhookAddressAllowedPlainAddresses drives webhookAddressAllowed
// directly with addresses that carry nothing for decodeEmbeddedIPv4 to unwrap
// (decoded == nil for every case here). That is deliberate: it is the only way
// to distinguish `decoded != nil` from a `decoded == nil` mutant, since for
// every one of these inputs the mutant would zero out `ip` before
// classification, and — because a nil net.IP is neither IsLoopback() nor
// isBlockedPrivate() — every refusal below would flip to "allowed" under the
// mutant, while a genuinely-embedded address (tested via CheckWebhookDestination
// in server_egress_test.go / server_webhooks_test.go) would not visibly differ.
func TestWebhookAddressAllowedPlainAddresses(t *testing.T) {
	cases := []struct {
		name          string
		ip            string
		allowLoopback bool
		wantLoopback  bool
		wantAllowed   bool
	}{
		{"loopback allowed", "127.0.0.1", true, true, true},
		{"loopback refused", "127.0.0.1", false, true, false},
		{"loopback v6 refused", "::1", false, true, false},
		{"private refused regardless of allowLoopback", "10.1.2.3", true, false, false},
		{"link-local refused", "169.254.1.1", true, false, false},
		{"public allowed", "93.184.216.34", true, false, true},
		{"public allowed, loopback disallowed", "93.184.216.34", false, false, true},
		{"metadata refused even with allowLoopback", "169.254.169.254", true, false, false},
	}
	for _, c := range cases {
		ip := net.ParseIP(c.ip)
		if ip == nil {
			t.Fatalf("%s: test fixture %q did not parse", c.name, c.ip)
		}
		gotLoopback, gotAllowed := webhookAddressAllowed(ip, c.allowLoopback)
		if gotLoopback != c.wantLoopback || gotAllowed != c.wantAllowed {
			t.Errorf("%s: webhookAddressAllowed(%s, allowLoopback=%v) = (%v, %v), want (%v, %v)",
				c.name, c.ip, c.allowLoopback, gotLoopback, gotAllowed, c.wantLoopback, c.wantAllowed)
		}
	}
}

// TestCheckWebhookDestinationSchemeAndHostGuards drives CheckWebhookDestination
// end to end across its early-refusal branches: non-http(s) scheme, a hostless
// URL, the GCE metadata hostname (including the trailing-dot/case-insensitive
// forms), and "localhost" / "*.localhost" under both allowLoopback settings.
func TestCheckWebhookDestinationSchemeAndHostGuards(t *testing.T) {
	guard := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return []string{"93.184.216.34"}, nil
	}, nil)
	ctx := context.Background()

	cases := []struct {
		name          string
		url           string
		allowLoopback bool
		wantErr       bool
		wantLoopback  bool
	}{
		{"ws scheme refused", "ws://hook.example/x", false, true, false},
		{"file scheme refused", "file:///etc/passwd", false, true, false},
		{"hostless refused", "http:///path", false, true, false},
		{"gce metadata hostname refused", "http://metadata.google.internal/x", true, true, false},
		{"gce metadata hostname, trailing dot + case", "http://METADATA.Google.Internal./x", true, true, false},
		{"localhost refused by default", "http://localhost/x", false, true, true},
		{"localhost allowed when configured", "http://localhost/x", true, false, true},
		{"dotted localhost refused by default", "http://api.localhost/x", false, true, true},
		{"dotted localhost allowed when configured", "http://api.localhost/x", true, false, true},
		{"ordinary https host resolves and is allowed", "https://hook.example/x", false, false, false},
	}
	for _, c := range cases {
		loopback, err := guard.CheckWebhookDestination(ctx, c.url, c.allowLoopback)
		if (err != nil) != c.wantErr {
			t.Errorf("%s: err = %v, wantErr %v", c.name, err, c.wantErr)
		}
		if loopback != c.wantLoopback {
			t.Errorf("%s: loopback = %v, want %v", c.name, loopback, c.wantLoopback)
		}
	}
}

// TestCheckWebhookDestinationResolutionFailureFailsClosed covers the
// addressesFor-error and unparseable-resolved-address arms.
func TestCheckWebhookDestinationResolutionFailureFailsClosed(t *testing.T) {
	nx := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, nil // empty, non-error result: also unresolved
	}, nil)
	if _, err := nx.CheckWebhookDestination(context.Background(), "https://nx.example/x", false); err == nil {
		t.Error("an unresolvable host must fail closed")
	}

	garbage := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return []string{"not-an-ip"}, nil
	}, nil)
	if _, err := garbage.CheckWebhookDestination(context.Background(), "https://garbage.example/x", false); err == nil {
		t.Error("a resolver answering with an unparseable address must fail closed")
	}
}
