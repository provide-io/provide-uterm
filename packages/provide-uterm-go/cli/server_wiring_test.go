//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// wiringConn is a browser conn carrying a pre-resolved principal, mirroring the
// production server/ws_conn.go browserConn.UtermPrincipal shape (a *hub.Principal
// whose Roles come from the authenticated principal). The hub reads it through
// its principalCarrier seam.
type wiringConn struct{ principal *hub.Principal }

func (c *wiringConn) UtermPrincipal() any { return c.principal }

// TestEventBusDropMetricReachesMetricsRegistry proves the event bus built by the
// production factory has the server metric sink wired: a subscriber whose queue
// overflows must land event_bus_subscriber_drop_total in the same *Metrics that
// /api/metrics serves. Without the sink the drop is counted on the subscription
// but silently discarded server-wide.
func TestEventBusDropMetricReachesMetricsRegistry(t *testing.T) {
	b := buildForRateLimits(t, nil)
	bus := b.registry.EventBus()
	if bus == nil {
		t.Fatal("factory must wire an EventBus into the registry")
	}
	sub, remove, err := bus.Watch("w-drop", nil, nil)
	if err != nil {
		t.Fatalf("Watch: %v", err)
	}
	defer remove()

	// One event past the queue depth forces exactly the ring-buffer drop path.
	for i := 0; i <= cap(sub.Queue); i++ {
		bus.Enqueue("w-drop", map[string]any{"type": "output"})
	}
	if sub.Dropped() == 0 {
		t.Fatalf("expected the subscriber queue to overflow, dropped = 0")
	}
	got := b.srv.Metrics().Snapshot()["event_bus_subscriber_drop_total"]
	if got != sub.Dropped() {
		t.Errorf("event_bus_subscriber_drop_total = %d, want %d (the bus drops)", got, sub.Dropped())
	}
}

// policyRoleFor boots the factory with the given auth mutation and asks the hub
// which role a principal holding only a "roles" claim set resolves to.
func policyRoleFor(t *testing.T, mutate func(*serverconfig.UtermServerConfig)) string {
	t.Helper()
	b := buildForRateLimits(t, mutate)
	ws := &wiringConn{principal: &hub.Principal{
		SubjectID: "u1",
		// Delegated roles say admin; the claims (what a non-delegating
		// deployment maps from) say nothing, so the two modes disagree.
		Roles:  map[string]bool{"admin": true},
		Claims: map[string]any{},
	}}
	pc, err := b.hub.State.PreparePolicyContext(context.Background(), ws, "w1", nil)
	if err != nil {
		t.Fatalf("PreparePolicyContext: %v", err)
	}
	if pc.Role == nil {
		t.Fatal("policy context carries no role")
	}
	return *pc.Role
}

// TestDelegateRolesDefaultPassesPrincipalRoles pins today's default: with
// auth.delegate_roles unset (true) a principal's own roles are honoured.
func TestDelegateRolesDefaultPassesPrincipalRoles(t *testing.T) {
	if got := policyRoleFor(t, nil); got != "admin" {
		t.Errorf("default delegate_roles role = %q, want %q", got, "admin")
	}
}

// TestDelegateRolesFalseIgnoresPrincipalRoles proves auth.delegate_roles = false
// actually disables role passthrough. This is a privilege control: a deployment
// that turns delegation off must not hand out admin because the principal
// claimed it — only an admin/operator claim may.
func TestDelegateRolesFalseIgnoresPrincipalRoles(t *testing.T) {
	got := policyRoleFor(t, func(c *serverconfig.UtermServerConfig) {
		c.Auth.DelegateRoles = false
	})
	if got != "viewer" {
		t.Errorf("delegate_roles=false role = %q, want %q (no admin claim present)", got, "viewer")
	}
}
