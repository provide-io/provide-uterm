//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical

import "testing"

func TestParseLitevirtEndpoint(t *testing.T) {
	cases := []struct {
		name     string
		in       *string
		wantHost string
		wantPort int
		wantErr  bool
	}{
		{"host_port", ptr("vm.local:9000"), "vm.local", 9000, false},
		{"dns_prefix", ptr("dns:///vm.local:9001"), "vm.local", 9001, false},
		{"ipv4", ptr("127.0.0.1:50051"), "127.0.0.1", 50051, false},
		{"nil", nil, "", 0, true},
		{"blank", ptr("   "), "", 0, true},
		{"no_port", ptr("vm.local"), "", 0, true},
		{"bad_port", ptr("vm.local:nope"), "", 0, true},
		{"port_too_high", ptr("vm.local:70000"), "", 0, true},
		{"port_zero", ptr("vm.local:0"), "", 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			host, port, err := ParseLitevirtEndpoint(tc.in)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got %s:%d", host, port)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if host != tc.wantHost || port != tc.wantPort {
				t.Fatalf("got %s:%d want %s:%d", host, port, tc.wantHost, tc.wantPort)
			}
		})
	}
}

func TestValidateLitevirtNormalizesEndpoint(t *testing.T) {
	d := NewDefinition()
	d.TargetID = "gt-lv"
	d.Protocol = "LITEVIRT"
	d.Endpoint = ptr("dns:///vm.local:9000")
	d.Config = map[string]any{"vm_name": "vm1"}
	if err := d.Validate(); err != nil {
		t.Fatalf("validate: %v", err)
	}
	if d.Protocol != "litevirt" {
		t.Fatalf("protocol not lowercased: %q", d.Protocol)
	}
	if d.Endpoint == nil || *d.Endpoint != "vm.local:9000" {
		t.Fatalf("endpoint not normalized: %v", d.Endpoint)
	}
}

func TestValidateLitevirtMissingEndpoint(t *testing.T) {
	d := NewDefinition()
	d.TargetID = "gt-lv"
	d.Protocol = "litevirt"
	d.Endpoint = nil
	err := d.Validate()
	ge, ok := err.(*Error)
	if !ok || ge.Code != CodeInvalid || ge.Message != "endpoint is required for protocol litevirt" {
		t.Fatalf("got %v want litevirt endpoint required", err)
	}
}

func TestConfigRoundTripCloneAndPublicCopy(t *testing.T) {
	d := NewDefinition()
	d.TargetID = "gt-lv"
	d.TenantID = "acme"
	d.Protocol = "litevirt"
	d.Endpoint = ptr("vm.local:9000")
	d.Config = map[string]any{"vm_name": "vm1"}

	// Config survives Clone and is not aliased.
	clone := d.Clone()
	if clone.Config["vm_name"] != "vm1" {
		t.Fatalf("clone lost config: %+v", clone.Config)
	}
	clone.Config["vm_name"] = "changed"
	if d.Config["vm_name"] != "vm1" {
		t.Fatalf("clone aliased original config")
	}

	// Config is NOT a secret: PublicCopy retains it.
	pub := d.PublicCopy()
	if pub.Config["vm_name"] != "vm1" {
		t.Fatalf("public copy dropped config: %+v", pub.Config)
	}
}
