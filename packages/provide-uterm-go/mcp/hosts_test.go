//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import "testing"

func TestIsInternalHostBlocked(t *testing.T) {
	internal := []string{
		"localhost", "localhost.", "LOCALHOST", "metadata", "metadata.google.internal",
		"foo.localhost", "127.0.0.1", "127.1", "[::1]", "169.254.169.254",
		"10.0.0.1", "192.168.1.1", "172.16.5.4", "0.0.0.0", "192.0.2.9",
		"198.51.100.7", "203.0.113.2", "240.0.0.1", "fc00::1", "fe80::1",
		// non-canonical numeric IPv4 forms that resolvers accept.
		"2130706433", "0x7f.1", "0177.0.0.1",
	}
	for _, h := range internal {
		if !isInternalHost(h) {
			t.Errorf("expected %q classified internal", h)
		}
	}
}

func TestIsInternalHostPublic(t *testing.T) {
	public := []string{
		"example.com", "8.8.8.8", "1.1.1.1", "93.184.216.34",
		"github.com", "2606:4700:4700::1111", "not-an-ip-or-metadata",
	}
	for _, h := range public {
		if isInternalHost(h) {
			t.Errorf("expected %q classified public", h)
		}
	}
}

func TestInetAton(t *testing.T) {
	cases := map[string]string{
		"2130706433": "127.0.0.1",
		"0x7f000001": "127.0.0.1",
		"127.1":      "127.0.0.1",
		"127.0.1":    "127.0.0.1",
		"0177.0.0.1": "127.0.0.1",
		"1.2.3.4":    "1.2.3.4",
	}
	for in, want := range cases {
		addr, ok := inetAton(in)
		if !ok || addr.String() != want {
			t.Errorf("inetAton(%q) = (%v,%v), want %s", in, addr, ok, want)
		}
	}
	bad := []string{"", "1.2.3.4.5", "example.com", "256.1", "1..2", "0x"}
	for _, in := range bad {
		if _, ok := inetAton(in); ok {
			t.Errorf("inetAton(%q) should fail", in)
		}
	}
}

func TestAllowPrivateHostsToggle(t *testing.T) {
	orig := AllowPrivateHosts
	defer func() { AllowPrivateHosts = orig }()
	AllowPrivateHosts = true
	if isInternalHost("10.0.0.1") {
		t.Fatalf("RFC1918 host should be permitted when AllowPrivateHosts is set")
	}
	// Loopback/link-local remain blocked regardless of the toggle.
	if !isInternalHost("127.0.0.1") {
		t.Fatalf("loopback must stay blocked even with AllowPrivateHosts")
	}
}
