//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"fmt"
	"net"
	"net/url"
	"strings"
)

// metadataIPs ports _net._METADATA_IPS — cloud-metadata service IPs that an
// outbound webhook must never reach.
var metadataIPs = []net.IP{
	net.ParseIP("169.254.169.254"),
	net.ParseIP("100.100.100.200"),
	net.ParseIP("fd00:ec2::254"),
}

// nat64WellKnown is the RFC 6052 NAT64 well-known prefix 64:ff9b::/96.
var _, nat64WellKnown, _ = net.ParseCIDR("64:ff9b::/96")

// EgressBlockedError ports egress.EgressBlockedError.
type EgressBlockedError struct{ msg string }

func (e *EgressBlockedError) Error() string { return e.msg }

// HostResolver resolves a hostname to IP strings; overridable in tests.
type HostResolver func(ctx context.Context, host string) ([]string, error)

func defaultResolver(ctx context.Context, host string) ([]string, error) {
	return net.DefaultResolver.LookupHost(ctx, host)
}

func isMetadataIP(ip net.IP) bool {
	// Decode any embedded-IPv4 IPv6 form so a wrapped metadata address can't
	// evade the membership check (mirrors _decode_embedded_ipv4).
	if decoded := decodeEmbeddedIPv4(ip); decoded != nil {
		ip = decoded
	}
	for _, m := range metadataIPs {
		if m != nil && m.Equal(ip) {
			return true
		}
	}
	return false
}

// decodeEmbeddedIPv4 ports egress._decode_embedded_ipv4 for the forms that
// carry a reachable IPv4: IPv4-mapped, 6to4, NAT64 well-known, and the
// deprecated IPv4-compatible form (excluding :: and ::1).
func decodeEmbeddedIPv4(ip net.IP) net.IP {
	if ip == nil {
		return nil
	}
	// IPv4-mapped ::ffff:a.b.c.d and plain IPv4 are already handled natively by
	// net.IP.Equal (which normalises 4-in-6), so To4() != nil needs no decode.
	if v4 := ip.To4(); v4 != nil {
		return nil
	}
	v6 := ip.To16()
	if v6 == nil {
		return nil
	}
	// 6to4 2002:AABB:CCDD::
	if v6[0] == 0x20 && v6[1] == 0x02 {
		return net.IPv4(v6[2], v6[3], v6[4], v6[5]).To4()
	}
	// NAT64 well-known 64:ff9b::/96
	if nat64WellKnown != nil && nat64WellKnown.Contains(ip) {
		return net.IPv4(v6[12], v6[13], v6[14], v6[15]).To4()
	}
	// IPv4-compatible ::a.b.c.d (high 96 bits zero), excluding :: and ::1.
	if isZeroPrefix(v6[:12]) {
		low := net.IPv4(v6[12], v6[13], v6[14], v6[15]).To4()
		excluded := v6[12] == 0 && v6[13] == 0 && v6[14] == 0 && (v6[15] == 0 || v6[15] == 1)
		if !excluded {
			return low
		}
	}
	return nil
}

func isZeroPrefix(b []byte) bool {
	for _, v := range b {
		if v != 0 {
			return false
		}
	}
	return true
}

// AssertWebhookTargetAllowed ports egress.assert_webhook_target_allowed: raise
// EgressBlockedError if the webhook URL host resolves to a cloud-metadata IP.
// Private/internal hosts ARE allowed. Resolution failure fails closed.
func AssertWebhookTargetAllowed(ctx context.Context, rawURL string, resolver HostResolver) error {
	if resolver == nil {
		resolver = defaultResolver
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return nil
	}
	host := parsed.Hostname()
	if host == "" {
		return nil
	}
	h := strings.Trim(strings.TrimSpace(host), "[]")

	var addresses []string
	if literal := net.ParseIP(h); literal != nil {
		addresses = []string{literal.String()}
	} else {
		resolved, rerr := resolver(ctx, h)
		if rerr != nil {
			return &EgressBlockedError{fmt.Sprintf("webhook target %q could not be resolved", rawURL)}
		}
		if len(resolved) == 0 {
			return &EgressBlockedError{fmt.Sprintf("webhook target %q could not be resolved", rawURL)}
		}
		addresses = resolved
	}
	for _, addr := range addresses {
		ip := net.ParseIP(addr)
		if ip == nil {
			continue
		}
		if isMetadataIP(ip) {
			return &EgressBlockedError{
				fmt.Sprintf("webhook target %q resolves to a blocked metadata address", rawURL)}
		}
	}
	return nil
}
