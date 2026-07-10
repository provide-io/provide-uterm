//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestJWTEmptySubjectRejected(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	now := time.Now().Unix()
	tok, _ := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub": "", "iss": "provide-uterm", "aud": "provide-uterm-server", "exp": now + 600,
	}).SignedString([]byte(testKey))
	if _, err := idp.PrincipalFromJWTToken(tok); err == nil {
		t.Error("empty sub accepted")
	}
}

func TestJWTNegativeSkewClamped(t *testing.T) {
	cfg := jwtAuthConfig()
	cfg.ClockSkewSeconds = -5 // clamped to 0
	idp := NewLocalIdentityProvider(cfg, nil)
	if _, err := idp.PrincipalFromJWTToken(makeToken(t, "u", []string{"admin"}, 600)); err != nil {
		t.Errorf("negative skew broke validation: %v", err)
	}
}

func TestEgressDefaultResolverAndErrorString(t *testing.T) {
	// nil resolver + DNS name → uses defaultResolver (localhost resolves locally).
	if err := AssertWebhookTargetAllowed(context.Background(), "https://localhost/x", nil); err != nil {
		t.Errorf("localhost via default resolver blocked: %v", err)
	}
	// Error() string
	e := &EgressBlockedError{msg: "blocked"}
	if e.Error() != "blocked" {
		t.Errorf("Error() = %q", e.Error())
	}
	// mapped metadata literal is blocked natively.
	var blocked *EgressBlockedError
	err := AssertWebhookTargetAllowed(context.Background(), "https://[::ffff:169.254.169.254]/x", nil)
	if err == nil || err.Error() == "" {
		t.Errorf("mapped metadata not blocked: %v", err)
	}
	_ = blocked
}

func TestWebhookIDPMetadataURLBlocked(t *testing.T) {
	// URL host resolves to a metadata IP → egress guard fails → deny → nil.
	idp, err := NewWebhookIdentityProvider("https://169.254.169.254/resolve", WebhookIDPOptions{})
	if err != nil {
		t.Fatal(err)
	}
	idp.now = func() float64 { return 1e6 }
	if p, err := idp.Authenticate(context.Background(), &Request{}); err != nil || p != nil {
		t.Errorf("metadata URL not blocked: %v %+v", err, p)
	}
}

func TestDecodeEmbeddedIPv4CompatAndFallthrough(t *testing.T) {
	// IPv4-compatible ::255.1.2.3 (non-excluded) → decodes.
	if ip := decodeEmbeddedIPv4(net.ParseIP("::255.1.2.3")); ip == nil || ip.String() != "255.1.2.3" {
		t.Errorf("compat decode = %v", ip)
	}
	// Non-embedded global IPv6 → nil (isZeroPrefix false path).
	if ip := decodeEmbeddedIPv4(net.ParseIP("2001:db8::1")); ip != nil {
		t.Errorf("global v6 decode = %v", ip)
	}
}

func TestResolvedTokenPathDefault(t *testing.T) {
	t.Setenv("UTERM_DEV_TOKEN_PATH", "")
	got := resolvedTokenPath("")
	home, _ := os.UserHomeDir()
	if got != filepath.Join(home, ".cache", "uterm", "dev_token") {
		t.Errorf("default token path = %q", got)
	}
}

func TestSetupDevIDPKeepsExistingWorkerToken(t *testing.T) {
	dir := t.TempDir()
	existing := "preexisting-worker-token-32-characters"
	auth := serverconfig.AuthConfig{Mode: "dev_token", JWTRolesClaim: "roles",
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", WorkerBearerToken: &existing}
	if _, err := SetupDevIDP(&auth, DevIDPOptions{TokenPath: filepath.Join(dir, "t")}); err != nil {
		t.Fatal(err)
	}
	if auth.WorkerBearerToken == nil || *auth.WorkerBearerToken != existing {
		t.Errorf("existing worker token overwritten: %v", auth.WorkerBearerToken)
	}
}
