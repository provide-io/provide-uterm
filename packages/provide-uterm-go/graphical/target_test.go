//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical

import "testing"

func ptr(s string) *string { return &s }

func TestParseRfbEndpoint(t *testing.T) {
	cases := []struct {
		name     string
		in       *string
		wantHost string
		wantPort int
		wantErr  bool
	}{
		{"host_port", ptr("vm.local:5900"), "vm.local", 5900, false},
		{"rfb_scheme", ptr("rfb://vm.local:5901"), "vm.local", 5901, false},
		{"dns_prefix", ptr("dns:///vm.local:5902"), "vm.local", 5902, false},
		{"dns_prefix_rfb", ptr("dns:///rfb://vm.local:5903"), "vm.local", 5903, false},
		{"nil", nil, "", 0, true},
		{"blank", ptr("   "), "", 0, true},
		{"no_colon", ptr("vm.local"), "", 0, true},
		{"no_port", ptr("rfb://vm.local"), "", 0, true},
		{"bad_port", ptr("vm.local:notaport"), "", 0, true},
		{"port_too_high", ptr("vm.local:70000"), "", 0, true},
		{"port_zero", ptr("vm.local:0"), "", 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			host, port, err := ParseRfbEndpoint(tc.in)
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

func TestValidate(t *testing.T) {
	base := func() *Definition {
		d := NewDefinition()
		d.TargetID = "gt-1"
		d.Endpoint = ptr("vm.local:5900")
		return d
	}

	t.Run("ok_rfb_normalizes_endpoint", func(t *testing.T) {
		d := base()
		d.Protocol = "RFB"
		if err := d.Validate(); err != nil {
			t.Fatalf("validate: %v", err)
		}
		if d.Protocol != "rfb" {
			t.Fatalf("protocol not lowercased: %q", d.Protocol)
		}
		if d.Endpoint == nil || *d.Endpoint != "vm.local:5900" {
			t.Fatalf("endpoint not normalized: %v", d.Endpoint)
		}
	})

	t.Run("memory_protocol_skips_endpoint", func(t *testing.T) {
		d := base()
		d.Protocol = "memory"
		d.Endpoint = nil
		if err := d.Validate(); err != nil {
			t.Fatalf("memory validate: %v", err)
		}
	})

	bad := []struct {
		name string
		mut  func(*Definition)
		msg  string
	}{
		{"bad_target_id", func(d *Definition) { d.TargetID = "bad id!" }, "target_id must be a safe identifier"},
		{"bad_protocol", func(d *Definition) { d.Protocol = "vnc" }, "unsupported protocol"},
		{"rfb_missing_endpoint", func(d *Definition) { d.Endpoint = nil }, "endpoint is required for protocol rfb"},
		{"width_low", func(d *Definition) { d.Width = 0 }, "width out of range"},
		{"width_high", func(d *Definition) { d.Width = 9000 }, "width out of range"},
		{"height_low", func(d *Definition) { d.Height = 0 }, "height out of range"},
		{"height_high", func(d *Definition) { d.Height = 9000 }, "height out of range"},
		{"bad_tenant", func(d *Definition) { d.TenantID = "bad tenant!" }, "tenant_id is invalid"},
		{"bad_secret_ref", func(d *Definition) { d.CaSecretRef = ptr("http://evil") }, "invalid secret reference syntax"},
	}
	for _, tc := range bad {
		t.Run(tc.name, func(t *testing.T) {
			d := base()
			tc.mut(d)
			err := d.Validate()
			if err == nil {
				t.Fatalf("expected error")
			}
			ge, ok := err.(*Error)
			if !ok || ge.Code != CodeInvalid || ge.Message != tc.msg {
				t.Fatalf("got %v want CodeInvalid %q", err, tc.msg)
			}
		})
	}

	t.Run("valid_secret_refs", func(t *testing.T) {
		d := base()
		d.CaSecretRef = ptr("env:CA_BUNDLE")
		d.ClientCertSecretRef = ptr("file:/certs/client.pem")
		d.ClientKeySecretRef = ptr("env:CLIENT_KEY")
		d.TenantID = "acme"
		if err := d.Validate(); err != nil {
			t.Fatalf("validate: %v", err)
		}
	})
}

func TestPublicCopyStripsSecrets(t *testing.T) {
	d := NewDefinition()
	d.TargetID = "gt-1"
	d.TenantID = "acme"
	d.Secret = ptr("hunter2")
	d.CaSecretRef = ptr("env:CA")
	d.ClientCertSecretRef = ptr("env:CERT")
	d.ClientKeySecretRef = ptr("env:KEY")
	d.Endpoint = ptr("vm:5900")
	d.CreatedBy = ptr("alice")

	pub := d.PublicCopy()
	if pub.Secret != nil || pub.CaSecretRef != nil || pub.ClientCertSecretRef != nil || pub.ClientKeySecretRef != nil { // pragma: allowlist secret
		t.Fatalf("secrets not stripped: %+v", pub)
	}
	// Non-secret fields survive.
	if pub.Endpoint == nil || *pub.Endpoint != "vm:5900" || pub.TenantID != "acme" {
		t.Fatalf("public copy lost data: %+v", pub)
	}
	// Original is untouched (deep copy).
	if d.Secret == nil || *d.Secret != "hunter2" { // pragma: allowlist secret
		t.Fatalf("original mutated")
	}
	// Mutating the clone must not alias the original.
	*pub.Endpoint = "changed"
	if *d.Endpoint != "vm:5900" {
		t.Fatalf("clone aliased original endpoint")
	}
}
