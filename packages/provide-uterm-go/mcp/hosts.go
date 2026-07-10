//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"net/netip"
	"strconv"
	"strings"
)

// SSRF host classification. Port of provide.uterm.ai.server_validators
// _is_internal_host / _numeric_ipv4. Literal IPs are classified against the
// same IETF ranges Python's ipaddress module uses; hostnames are matched
// against a small metadata denylist. No DNS lookup is performed here —
// rebinding and egress control remain the server's responsibility.

// blockedHostNames are hostnames that resolve to cloud metadata / internal-only
// endpoints, refused by name without a DNS lookup.
var blockedHostNames = map[string]struct{}{
	"localhost":                {},
	"metadata.google.internal": {},
	"metadata":                 {},
}

// mustPrefix parses a CIDR that is known-valid at init time.
func mustPrefix(s string) netip.Prefix {
	p, err := netip.ParsePrefix(s)
	if err != nil {
		panic("mcp: invalid built-in prefix " + s + ": " + err.Error())
	}
	return p
}

// mustPrefixes parses a slice of known-valid CIDRs.
func mustPrefixes(cidrs ...string) []netip.Prefix {
	out := make([]netip.Prefix, 0, len(cidrs))
	for _, c := range cidrs {
		out = append(out, mustPrefix(c))
	}
	return out
}

// loopbackLinkLocal are the ranges Python classifies via is_loopback /
// is_link_local; membership always marks a host internal.
var loopbackLinkLocal = mustPrefixes(
	"127.0.0.0/8", "::1/128", // loopback (v4, v6)
	"169.254.0.0/16", "fe80::/10", // link-local (v4, v6)
)

// privateReservedUnspecified are the ranges Python classifies via
// is_private / is_reserved / is_unspecified; membership marks a host internal
// unless AllowPrivateHosts is set. The lists mirror ipaddress's private and
// reserved network tables plus the unspecified addresses.
var privateReservedUnspecified = mustPrefixes(
	// is_unspecified.
	"0.0.0.0/32", "::/128",
	// IPv4 is_private (RFC1918 + IANA special-use, minus ranges already
	// covered by loopback/link-local above).
	"0.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.0.0.0/29",
	"192.0.0.170/31", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
	"198.51.100.0/24", "203.0.113.0/24", "255.255.255.255/32",
	// IPv4 is_reserved (class E).
	"240.0.0.0/4",
	// IPv6 is_private.
	"::ffff:0:0/96", "100::/64", "2001::/23", "2001:2::/48",
	"2001:db8::/32", "2001:10::/28", "fc00::/7",
	// IPv6 is_reserved.
	"::/8", "100::/8", "200::/7", "400::/6", "800::/5", "1000::/4",
	"4000::/3", "6000::/3", "8000::/3", "a000::/3", "c000::/3",
	"e000::/4", "f000::/5", "f800::/6", "fe00::/9",
)

// addrInAny reports whether ip is contained in any of prefixes. Prefix.Contains
// is family-aware (a v4 address is never contained by a v6 prefix and vice
// versa), matching how Python's ipaddress classifies an IPv4Address only
// against the v4 tables and an IPv6Address (including ::ffff: mapped forms)
// only against the v6 tables.
func addrInAny(ip netip.Addr, prefixes []netip.Prefix) bool {
	for _, p := range prefixes {
		if p.Contains(ip) {
			return true
		}
	}
	return false
}

// classifyIP reports whether ip targets an internal / metadata endpoint.
func classifyIP(ip netip.Addr) bool {
	if addrInAny(ip, loopbackLinkLocal) {
		return true
	}
	if !AllowPrivateHosts && addrInAny(ip, privateReservedUnspecified) {
		return true
	}
	return false
}

// parseInetNumber parses a single inet_aton component: 0x/0X hex, leading-0
// octal, or decimal. Returns the value and whether it parsed cleanly.
func parseInetNumber(p string) (uint64, bool) {
	if p == "" {
		return 0, false
	}
	base := 10
	digits := p
	switch {
	case len(p) >= 2 && p[0] == '0' && (p[1] == 'x' || p[1] == 'X'):
		base, digits = 16, p[2:]
		if digits == "" {
			return 0, false
		}
	case len(p) >= 2 && p[0] == '0':
		base, digits = 8, p[1:]
	}
	v, err := strconv.ParseUint(digits, base, 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

// inetAton replicates the C resolver's inet_aton: it accepts the non-canonical
// numeric IPv4 forms (decimal 2130706433, octal 0177.0.0.1, hex 0x7f.1, and the
// shortened 127.1 forms) that netip.ParseAddr rejects but sockets / httpx /
// curl accept. Returns (addr, true) on success, or (_, false) for anything that
// is not a purely numeric IPv4 form (i.e. a real hostname). Never performs DNS.
func inetAton(s string) (netip.Addr, bool) {
	if s == "" {
		return netip.Addr{}, false
	}
	parts := strings.Split(s, ".")
	if len(parts) > 4 {
		return netip.Addr{}, false
	}
	vals := make([]uint64, len(parts))
	for i, p := range parts {
		v, ok := parseInetNumber(p)
		if !ok {
			return netip.Addr{}, false
		}
		vals[i] = v
	}
	var n uint64
	switch len(parts) {
	case 1:
		n = vals[0]
	case 2:
		if vals[0] > 0xff || vals[1] > 0xffffff {
			return netip.Addr{}, false
		}
		n = vals[0]<<24 | vals[1]
	case 3:
		if vals[0] > 0xff || vals[1] > 0xff || vals[2] > 0xffff {
			return netip.Addr{}, false
		}
		n = vals[0]<<24 | vals[1]<<16 | vals[2]
	case 4:
		for _, v := range vals {
			if v > 0xff {
				return netip.Addr{}, false
			}
		}
		n = vals[0]<<24 | vals[1]<<16 | vals[2]<<8 | vals[3]
	}
	if n > 0xffffffff {
		return netip.Addr{}, false
	}
	return netip.AddrFrom4([4]byte{byte(n >> 24), byte(n >> 16), byte(n >> 8), byte(n)}), true
}

// isInternalHost reports whether host targets an internal / metadata endpoint.
func isInternalHost(host string) bool {
	// Strip a trailing root dot ("localhost." == "localhost") and surrounding
	// brackets before matching, mirroring the Python normalisation.
	candidate := strings.ToLower(strings.TrimRight(strings.Trim(strings.TrimSpace(host), "[]"), "."))
	if _, ok := blockedHostNames[candidate]; ok {
		return true
	}
	// RFC 6761: "localhost" and any "*.localhost" name is reserved for loopback.
	if strings.HasSuffix(candidate, ".localhost") {
		return true
	}
	ip, err := netip.ParseAddr(candidate)
	if err != nil {
		// Not a canonical IP string. It may still be a non-canonical numeric
		// IPv4 form that resolvers accept — normalise and re-check.
		numeric, ok := inetAton(candidate)
		if !ok {
			return false
		}
		ip = numeric
	}
	return classifyIP(ip)
}
