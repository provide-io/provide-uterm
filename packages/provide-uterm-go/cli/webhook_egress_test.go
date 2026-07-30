//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// egressSessionID is the session every webhook test registers against: the
// default shell definition, with auto-start disabled so nothing is spawned.
const egressSessionID = "provide-shell"

// buildForWebhooks assembles a server from a default config through the
// production factory. Nothing here hand-builds server.Deps: the whole point of
// this file is that a key reaching the guard depends on the factory wiring it,
// which a hand-built Deps would paper over.
func buildForWebhooks(t *testing.T, mutate func(*serverconfig.UtermServerConfig)) *serverBundle {
	t.Helper()
	return buildForWebhooksWithResolver(t, mutate, nil)
}

// call issues an authenticated JSON request against the assembled handler and
// returns the status plus decoded body.
func call(t *testing.T, b *serverBundle, method, path string, body map[string]any) (int, map[string]any) {
	t.Helper()
	var reader *bytes.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			t.Fatalf("marshal body: %v", err)
		}
		reader = bytes.NewReader(raw)
	} else {
		reader = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, reader)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+b.devToken)
	rec := httptest.NewRecorder()
	b.srv.Handler().ServeHTTP(rec, req)
	decoded := map[string]any{}
	_ = json.Unmarshal(rec.Body.Bytes(), &decoded)
	return rec.Code, decoded
}

// registerWebhook POSTs a webhook registration for the default session.
func registerWebhook(t *testing.T, b *serverBundle, url string) (int, map[string]any) {
	t.Helper()
	return call(t, b, http.MethodPost, "/api/sessions/"+egressSessionID+"/webhooks",
		map[string]any{"url": url})
}

// TestWebhookRoutesAreWiredByTheFactory is the first thing that has to be true
// before any egress rule can matter: the production factory must hand the
// server a webhook manager. Without one every session-webhook route answers
// 503, so `webhooks.allow_loopback_destinations` has nowhere to arrive and the
// SSRF guard it is supposed to govern is never consulted at all.
//
// A public literal destination is used so the only thing that can fail is the
// wiring, not the classifier.
func TestWebhookRoutesAreWiredByTheFactory(t *testing.T) {
	b := buildForWebhooks(t, nil)
	status, body := registerWebhook(t, b, "http://93.184.216.34/hook")
	if status == http.StatusServiceUnavailable {
		t.Fatalf("POST webhooks = 503 %v; the factory did not wire Deps.Webhooks", body)
	}
	if status != http.StatusOK {
		t.Fatalf("POST webhooks = %d %v, want 200", status, body)
	}
	if body["webhook_id"] == "" || body["webhook_id"] == nil {
		t.Errorf("registration response carries no webhook_id: %v", body)
	}
	if body["session_id"] != egressSessionID {
		t.Errorf("session_id = %v, want %q", body["session_id"], egressSessionID)
	}
}

// routableBind makes the deployment listen on every interface, which is the
// condition under which §3's bind term stops vouching for loopback.
func routableBind(c *serverconfig.UtermServerConfig) {
	c.Server.Host = "0.0.0.0"
}

// allowLoopbackKey sets webhooks.allow_loopback_destinations.
func allowLoopbackKey(c *serverconfig.UtermServerConfig) {
	c.Webhooks.AllowLoopbackDestinations = true
}

// both composes two config mutations.
func both(a, b func(*serverconfig.UtermServerConfig)) func(*serverconfig.UtermServerConfig) {
	return func(c *serverconfig.UtermServerConfig) { a(c); b(c) }
}

// assertRegistration drives one §6 row: build a server with mutate applied,
// register dest, and require the expected verdict.
func assertRegistration(
	t *testing.T, name string, mutate func(*serverconfig.UtermServerConfig), dest string, wantAccepted bool,
) {
	t.Helper()
	b := buildForWebhooks(t, mutate)
	status, body := registerWebhook(t, b, dest)
	switch {
	case wantAccepted && status != http.StatusOK:
		t.Errorf("%s: register %q = %d %v, want 200 (accepted)", name, dest, status, body)
	case !wantAccepted && status != http.StatusUnprocessableEntity:
		t.Errorf("%s: register %q = %d %v, want 422 (refused)", name, dest, status, body)
	}
}

// loopbackDestinations are the forms a loopback destination arrives in. The
// hostname forms matter as much as the literal: "localhost" and "*.localhost"
// are loopback by RFC 6761 regardless of what a resolver says.
var loopbackDestinations = []string{
	"http://127.0.0.1:9/hook",
	"http://127.0.0.5:9/hook", // 127/8, not just the one address
	"http://localhost:9/hook",
	"http://api.localhost:9/hook",
	"http://[::1]:9/hook",
}

// TestLoopbackBindAcceptsLoopbackDestinations is §6 row 1 and the reason §3 has
// a bind term at all. The default bind is 127.0.0.1, so without it the default
// configuration refuses loopback webhook destinations while listening only on
// loopback — a guard that protects nothing (no remote caller can reach the
// listener) at the cost of breaking every single-box deployment.
func TestLoopbackBindAcceptsLoopbackDestinations(t *testing.T) {
	for _, dest := range loopbackDestinations {
		assertRegistration(t, "loopback bind, key unset", nil, dest, true)
	}
}

// TestRoutableBindRefusesLoopbackDestinationsWithoutTheKey is §6 row 2: once the
// server listens on a routable interface, remote callers exist, and reaching
// 127.0.0.1 through the server converts "unreachable" into "reachable".
func TestRoutableBindRefusesLoopbackDestinationsWithoutTheKey(t *testing.T) {
	for _, dest := range loopbackDestinations {
		assertRegistration(t, "routable bind, key unset", routableBind, dest, false)
	}
}

// TestRoutableBindWithKeyAcceptsLoopbackDestinations is §6 row 3: the key reads
// as "*also* allow loopback on a routable bind", so it must be the thing that
// re-opens the case the bind term no longer covers.
func TestRoutableBindWithKeyAcceptsLoopbackDestinations(t *testing.T) {
	for _, dest := range loopbackDestinations {
		assertRegistration(t, "routable bind, key true", both(routableBind, allowLoopbackKey), dest, true)
	}
}

// alwaysRefused is every destination §1 refuses regardless of configuration:
// the three cloud-metadata addresses, the GCE metadata hostname, the private /
// link-local / reserved space, and an IPv6 wrapper carrying a metadata IPv4.
var alwaysRefused = []string{
	"http://169.254.169.254/latest/meta-data/",
	"http://100.100.100.200/latest/meta-data/",
	"http://[fd00:ec2::254]/latest/meta-data/",
	"http://metadata.google.internal/computeMetadata/v1/",
	"http://metadata.google.internal./computeMetadata/v1/", // trailing dot, same host
	"http://10.0.0.5/hook",
	"http://192.168.1.10/hook",
	"http://172.16.4.4/hook",
	"http://169.254.10.10/hook", // link-local generally, not just metadata
	"http://[64:ff9b::169.254.169.254]/hook",
	"http://[2002:a9fe:a9fe::1]/hook", // 6to4 wrapping 169.254.169.254
	"http://[fc00::1]/hook",           // IPv6 unique-local
	"http://[fe80::1]/hook",           // IPv6 link-local
}

// TestAlwaysRefusedDestinations covers §6 rows 4-8: there is deliberately no
// key that re-opens any of these, so each is asserted with the loopback key both
// unset and set, on both a loopback and a routable bind.
func TestAlwaysRefusedDestinations(t *testing.T) {
	binds := map[string]func(*serverconfig.UtermServerConfig){
		"loopback bind": nil,
		"routable bind": routableBind,
	}
	for bindName, bind := range binds {
		for _, keyed := range []bool{false, true} {
			mutate := bind
			label := bindName + ", key unset"
			if keyed {
				label = bindName + ", key true"
				if bind == nil {
					mutate = allowLoopbackKey
				} else {
					mutate = both(bind, allowLoopbackKey)
				}
			}
			for _, dest := range alwaysRefused {
				assertRegistration(t, label, mutate, dest, false)
			}
		}
	}
}

// TestPublicDestinationAccepted is the last §6 row, and it matters as much as
// the others: a guard that refuses everything passes every negative test above.
// A public literal is used so nothing but the classifier is under test.
func TestPublicDestinationAccepted(t *testing.T) {
	for _, dest := range []string{
		"http://93.184.216.34/hook",
		"https://93.184.216.34/hook",
		"https://[2606:2800:220:1:248:1893:25c8:1946]/hook",
	} {
		assertRegistration(t, "public destination", nil, dest, true)
		assertRegistration(t, "public destination, routable bind", routableBind, dest, true)
	}
}

// buildForWebhooksWithResolver is buildForWebhooks with the webhook guard's DNS
// resolver replaced (nil keeps the production resolver), so the §5 rows never
// depend on a real resolver's answer for a name somebody else controls.
func buildForWebhooksWithResolver(
	t *testing.T, mutate func(*serverconfig.UtermServerConfig), resolver server.EgressResolver,
) *serverBundle {
	t.Helper()
	cfg := serverconfig.DefaultServerConfig()
	// Keep one real session definition (the webhook routes 404 without one) but
	// never start it — these tests only exercise HTTP + the egress guard.
	cfg.Sessions = cfg.Sessions[:1]
	cfg.Sessions[0].SessionID = egressSessionID
	cfg.Sessions[0].AutoStart = false
	// dev_token mints a JWT the standard validator accepts, so the tests can
	// present a real admin bearer token instead of bypassing authentication.
	cfg.Auth.Mode = "dev_token"
	if mutate != nil {
		mutate(cfg)
	}
	bundle, err := buildServerFromConfig(context.Background(), cfg, "", withWebhookResolver(resolver))
	if err != nil {
		t.Fatalf("buildServerFromConfig: %v", err)
	}
	t.Cleanup(func() { _ = bundle.engine.Close(context.Background()) })
	t.Cleanup(bundle.webhooks.Shutdown)
	return bundle
}

// staticResolver answers every name with addrs.
func staticResolver(addrs ...string) server.EgressResolver {
	return func(context.Context, string) ([]string, error) { return addrs, nil }
}

// TestHostnameResolutionIsCheckedAndFailsClosed covers §6 rows 9-10 plus the
// positive control that keeps them honest. Resolution is what stops DNS-rebinding
// SSRF: a name is only as safe as every address it answers with, and a name that
// cannot be resolved has not been cleared — so failure and an empty answer are
// refusals, not passes.
func TestHostnameResolutionIsCheckedAndFailsClosed(t *testing.T) {
	cases := []struct {
		name         string
		resolver     server.EgressResolver
		wantAccepted bool
	}{
		{"resolves to private", staticResolver("10.0.0.5"), false},
		{"resolves to metadata", staticResolver("169.254.169.254"), false},
		{"resolves to loopback", staticResolver("127.0.0.1"), true}, // loopback bind vouches
		{"one of several is private", staticResolver("93.184.216.34", "192.168.0.9"), false},
		{"resolves to an embedded metadata IPv4", staticResolver("64:ff9b::a9fe:a9fe"), false},
		{"resolution fails", func(context.Context, string) ([]string, error) {
			return nil, errors.New("SERVFAIL")
		}, false},
		{"empty answer", staticResolver(), false},
		{"unparseable answer", staticResolver("not-an-address"), false},
		// The positive control: if the injected resolver were not consulted at
		// all, this row would fail (the real resolver cannot answer for
		// hook.invalid), which is what stops every row above from passing
		// vacuously.
		{"resolves to public", staticResolver("93.184.216.34"), true},
	}
	for _, tc := range cases {
		b := buildForWebhooksWithResolver(t, nil, tc.resolver)
		status, body := registerWebhook(t, b, "https://hook.invalid/path")
		accepted := status == http.StatusOK
		if accepted != tc.wantAccepted {
			t.Errorf("%s: register = %d %v, accepted=%v want accepted=%v",
				tc.name, status, body, accepted, tc.wantAccepted)
		}
	}
}

// deliveryProbe is a loopback HTTP destination that reports the deliveries it
// receives. It is a real listener on 127.0.0.1, so the guard classifies it as a
// genuine loopback destination rather than a stand-in for one.
func deliveryProbe(t *testing.T) (url string, received chan map[string]any) {
	t.Helper()
	received = make(chan map[string]any, 4)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		payload := map[string]any{}
		_ = json.NewDecoder(r.Body).Decode(&payload)
		received <- payload
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)
	return srv.URL + "/hook", received
}

// createTunnel mints a tunnel through the production route. The tunnel *is* a
// session, and the share it returns is the live-share condition §4 turns on.
func mintTunnelShare(t *testing.T, b *serverBundle) string {
	t.Helper()
	status, body := call(t, b, http.MethodPost, "/api/tunnels", map[string]any{"tunnel_type": "terminal"})
	if status != http.StatusOK {
		t.Fatalf("POST /api/tunnels = %d %v, want 200", status, body)
	}
	id, _ := body["tunnel_id"].(string)
	if id == "" {
		t.Fatalf("tunnel response carries no tunnel_id: %v", body)
	}
	return id
}

// registerWebhookFor registers a webhook on an arbitrary session.
func registerWebhookFor(t *testing.T, b *serverBundle, sessionID, url string) {
	t.Helper()
	status, body := call(t, b, http.MethodPost, "/api/sessions/"+sessionID+"/webhooks",
		map[string]any{"url": url})
	if status != http.StatusOK {
		t.Fatalf("register webhook on %s = %d %v, want 200 (§3 permits loopback on a loopback bind, "+
			"and §4 is a delivery-time rule, not a registration-time one)", sessionID, status, body)
	}
}

// waitForCounter polls a metric until it reaches want, or fails.
func waitForCounter(t *testing.T, b *serverBundle, name string, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		got := b.srv.Metrics().Snapshot()[name]
		if got >= want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%s = %d, want >= %d", name, got, want)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// TestLoopbackDeliveryRefusedWhileSessionHoldsLiveTunnelShare is §6 row 11.
//
// Tunnel sharing exposes a loopback-bound server through a relay, so for a
// shared session "bound to loopback" has stopped implying "only local callers
// exist" — the assumption §3's bind term rests on. Shares are issued at runtime,
// which is why this cannot be folded into the load-time permission: at config
// load the fact is neither true nor false yet.
func TestLoopbackDeliveryRefusedWhileSessionHoldsLiveTunnelShare(t *testing.T) {
	b := buildForWebhooks(t, nil)
	dest, received := deliveryProbe(t)
	tunnelID := mintTunnelShare(t, b)
	registerWebhookFor(t, b, tunnelID, dest)

	b.registry.EventBus().Enqueue(tunnelID, map[string]any{"type": "output", "data": map[string]any{}})

	// The refusal is what the operator sees, so the counter is the assertion —
	// and it also serialises the check: once it has ticked, the delivery
	// decision has definitely been made.
	waitForCounter(t, b, "webhook_delivery_blocked_tunnel_total", 1)
	select {
	case got := <-received:
		t.Fatalf("delivery reached a loopback destination for a tunnel-shared session: %v", got)
	case <-time.After(100 * time.Millisecond):
	}
}

// TestLoopbackDeliveryProceedsWithoutALiveTunnelShare is §6 row 12, in both of
// the ways a share stops being live. An expired share must not keep the guard
// closed: the token record outlives its expiry until the next sweep, so
// "a record exists" is not the question — "is a share live *now*" is.
func TestLoopbackDeliveryProceedsWithoutALiveTunnelShare(t *testing.T) {
	cases := map[string]func(t *testing.T, b *serverBundle, tunnelID string){
		"share revoked": func(t *testing.T, b *serverBundle, tunnelID string) {
			status, body := call(t, b, http.MethodDelete, "/api/tunnels/"+tunnelID+"/tokens", nil)
			if status != http.StatusOK {
				t.Fatalf("revoke tokens = %d %v, want 200", status, body)
			}
		},
		"share expired": func(_ *testing.T, b *serverBundle, tunnelID string) {
			// Age the record in place rather than waiting out a TTL: the
			// route clamps a tunnel TTL to >= 60s, so there is no legitimate
			// way to mint a share that expires inside a test. Epoch 0 is
			// unambiguously past for any wall clock.
			rec, _ := b.tunnelStore.GetToken(tunnelID)
			rec.ExpiresAt = 0
			b.tunnelStore.PutToken(tunnelID, rec)
		},
	}
	for name, expire := range cases {
		b := buildForWebhooks(t, nil)
		dest, received := deliveryProbe(t)
		tunnelID := mintTunnelShare(t, b)
		registerWebhookFor(t, b, tunnelID, dest)
		expire(t, b, tunnelID)

		b.registry.EventBus().Enqueue(tunnelID, map[string]any{"type": "output", "data": map[string]any{}})
		select {
		case got := <-received:
			if got["session_id"] != tunnelID {
				t.Errorf("%s: delivered payload session_id = %v, want %q", name, got["session_id"], tunnelID)
			}
		case <-time.After(3 * time.Second):
			t.Errorf("%s: no delivery arrived; an inactive share must not keep the guard closed", name)
		}
		if got := b.srv.Metrics().Snapshot()["webhook_delivery_blocked_tunnel_total"]; got != 0 {
			t.Errorf("%s: webhook_delivery_blocked_tunnel_total = %d, want 0", name, got)
		}
	}
}

// TestNonHTTPSchemeRefused pins that the deliverer's own precondition is
// enforced at registration: a file:// or gopher:// destination is not something
// a POST can reach, and both are classic SSRF primitives.
func TestNonHTTPSchemeRefused(t *testing.T) {
	for _, dest := range []string{
		"file:///etc/passwd",
		"gopher://93.184.216.34:70/x",
		"ws://93.184.216.34/hook",
		"http:///hook", // no host at all
	} {
		assertRegistration(t, "unusable scheme", nil, dest, false)
	}
}
