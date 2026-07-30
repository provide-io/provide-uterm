//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"fmt"
	"net"
	"net/url"
	"strings"
	"sync"
	"time"
)

// EgressGuard is the SSRF / DNS-rebinding egress guard. Port of
// provide.uterm.server.egress (+ _net). SECURITY-CRITICAL: the IP-block logic
// and fail-closed behavior mirror the Python guard exactly. Never weaken a
// check — cloud-metadata IPs are ALWAYS blocked; private/loopback/link-local/
// multicast/reserved/unspecified are blocked only when blockPrivate is set.
//
// The DNS resolver and clock are injected so tests never touch the network. The
// per-host resolve cache (used only by the webhook path, matching Python) is
// TTL-bounded; a bounded resolve timeout fails closed (an EgressBlockedError,
// never a hang or a 500).
type EgressGuard struct {
	resolver EgressResolver
	now      func() float64 // wall-clock seconds
	ttlS     float64
	timeout  time.Duration

	mu    sync.Mutex
	cache map[string]egressCacheEntry
}

type egressCacheEntry struct {
	at    float64
	addrs []string
}

// EgressResolver resolves a hostname to IP address strings. Overridable in
// tests; nil selects the default net.DefaultResolver-backed resolver.
type EgressResolver func(ctx context.Context, host string) ([]string, error)

// egressDNSTTLS bounds the per-host DNS cache. Port of _EGRESS_DNS_TTL_S: a
// rebind that flips a name to a metadata IP is caught on the next cache miss.
const egressDNSTTLS = 60.0

// egressResolveTimeout bounds one DNS resolution so a slow/hostile resolver
// can't hang a session-create or webhook check. Port of
// _EGRESS_RESOLVE_TIMEOUT_S; a timeout flows through the fail-closed path.
//
// Computed by a function (rather than a bare `const ... = 5 * time.Second`)
// so the multiplication is a statement inside a function body: a package-level
// const/var initializer executes before any test runs and carries no coverage
// counter, so a mutation there is permanently NOT_COVERED regardless of test
// quality. Wrapping it in a function gives the arithmetic a coverage counter
// (it still runs exactly once, at package init) that TestEgressResolveTimeout
// can then assert against.
var egressResolveTimeout = computeEgressResolveTimeout()

func computeEgressResolveTimeout() time.Duration { return 5 * time.Second }

// NewEgressGuard builds a guard. A nil resolver uses net.DefaultResolver; a nil
// clock uses the wall clock.
func NewEgressGuard(resolver EgressResolver, now func() float64) *EgressGuard {
	if resolver == nil {
		resolver = func(ctx context.Context, host string) ([]string, error) {
			return net.DefaultResolver.LookupHost(ctx, host)
		}
	}
	if now == nil {
		now = func() float64 { return float64(time.Now().UnixNano()) / 1e9 }
	}
	return &EgressGuard{
		resolver: resolver,
		now:      now,
		ttlS:     egressDNSTTLS,
		timeout:  egressResolveTimeout,
		cache:    map[string]egressCacheEntry{},
	}
}

// ── IP classification (metadata always / private-when-flag) ─────────────────

// metadataIPs ports _net._METADATA_IPS — cloud-metadata IPs an outbound
// connection must never reach. ALWAYS blocked regardless of blockPrivate.
var metadataIPs = []net.IP{
	net.ParseIP("169.254.169.254"),
	net.ParseIP("100.100.100.200"),
	net.ParseIP("fd00:ec2::254"),
}

var nat64WellKnown = mustCIDR("64:ff9b::/96")

// blockedPrivateV4 + blockedPrivateV6 are the exact union of CPython's
// ipaddress is_private / is_loopback / is_link_local / is_multicast /
// is_reserved / is_unspecified network constants (Python 3.13). The embedded
// IPv4-carrying IPv6 forms (::ffff:0.0.0.0/96 mapped, 2002::/16 6to4) are
// intentionally omitted here because decodeEmbeddedIPv4 rewrites them to their
// IPv4 before classification.
var blockedPrivateV4 = mustCIDRs(
	"0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
	"192.0.0.0/24", "192.0.0.170/31", "192.0.2.0/24", "192.168.0.0/16",
	"198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4",
	"255.255.255.255/32",
	"224.0.0.0/4", // multicast
	// RFC 6598 carrier-grade NAT. Deliberately NOT inherited from CPython:
	// ipaddress.ip_address("100.64.0.1").is_private is False, so a set derived
	// from is_private — as the rest of this list is — omits it. That is a gap in
	// the derivation rather than an allowance. CGNAT space carries real
	// infrastructure on carrier and container networks and is exactly what an
	// SSRF pivot wants. See conformance/EGRESS_GUARD.md §1.
	"100.64.0.0/10",
)

var blockedPrivateV6 = mustCIDRs(
	// is_private
	"::1/128", "::/128", "64:ff9b:1::/48", "100::/64", "2001::/23",
	"2001:db8::/32", "3fff::/20", "fc00::/7", "fe80::/10",
	// is_multicast
	"ff00::/8",
	// is_reserved
	"::/8", "100::/8", "200::/7", "400::/6", "800::/5", "1000::/4", "4000::/3",
	"6000::/3", "8000::/3", "a000::/3", "c000::/3", "e000::/4", "f000::/5",
	"f800::/6", "fe00::/9",
)

func mustCIDR(s string) *net.IPNet {
	_, n, err := net.ParseCIDR(s)
	if err != nil {
		panic("egress: bad CIDR " + s)
	}
	return n
}

func mustCIDRs(ss ...string) []*net.IPNet {
	out := make([]*net.IPNet, 0, len(ss))
	for _, s := range ss {
		out = append(out, mustCIDR(s))
	}
	return out
}

// decodeEmbeddedIPv4 ports egress._decode_embedded_ipv4: returns the IPv4
// carried by an IPv6 wrapper (IPv4-mapped, 6to4, NAT64 well-known, or the
// deprecated IPv4-compatible form excluding :: and ::1), else nil.
func decodeEmbeddedIPv4(ip net.IP) net.IP {
	if ip == nil {
		return nil
	}
	// IPv4 and IPv4-mapped ::ffff:a.b.c.d already normalise via To4().
	if v4 := ip.To4(); v4 != nil {
		return v4
	}
	v6 := ip.To16()
	if v6 == nil {
		return nil
	}
	// Deliberately an if-chain rather than a tagless `switch { case ...: }`: Go's
	// coverage instrumentation attributes a case body's counter starting *after*
	// the case guard, so the guard expression itself sits in a gap no block
	// covers — go-gremlins then reports a mutable comparison inside a guard
	// (the 0x20/0x02 6to4 prefix check below) as permanently NOT_COVERED
	// regardless of test quality. An if-condition does not have this gap (its
	// guard is part of the preceding block, which runs on every call), so
	// TestDecodeEmbeddedIPv4Forms's 6to4 case actually mutation-tests it. Every
	// arm still returns, so the if-chain is behaviorally identical to the switch.
	if v6[0] == 0x20 && v6[1] == 0x02 { // 6to4 2002::/16
		return net.IPv4(v6[2], v6[3], v6[4], v6[5]).To4()
	}
	if nat64WellKnown.Contains(ip) { // NAT64 well-known 64:ff9b::/96
		return net.IPv4(v6[12], v6[13], v6[14], v6[15]).To4()
	}
	if isZeroPrefix(v6[:12]) { // IPv4-compatible ::a.b.c.d (deprecated)
		if v6[12] == 0 && v6[13] == 0 && v6[14] == 0 && (v6[15] == 0 || v6[15] == 1) {
			return nil // :: and ::1 handled by the normal v6 branches
		}
		return net.IPv4(v6[12], v6[13], v6[14], v6[15]).To4()
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

func isMetadataIP(ip net.IP) bool {
	for _, m := range metadataIPs {
		if m != nil && m.Equal(ip) {
			return true
		}
	}
	return false
}

func isBlockedPrivate(ip net.IP) bool {
	nets := blockedPrivateV6
	if ip.To4() != nil {
		nets = blockedPrivateV4
	}
	for _, n := range nets {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

// checkResolvedIP ports egress._check_resolved_ip: decode any embedded IPv4,
// block metadata always, block private/loopback/etc when blockPrivate.
func checkResolvedIP(ip net.IP, blockPrivate bool, onMetadata, onPrivate string) error {
	if decoded := decodeEmbeddedIPv4(ip); decoded != nil {
		ip = decoded
	}
	if isMetadataIP(ip) {
		return &EgressBlockedError{Msg: onMetadata}
	}
	if blockPrivate && isBlockedPrivate(ip) {
		return &EgressBlockedError{Msg: onPrivate}
	}
	return nil
}

// ── resolution ──────────────────────────────────────────────────────────────

// resolveWithTimeout runs the injected resolver under a hard timeout, even if
// the resolver ignores context cancellation. A timeout returns an error so the
// caller fails closed (parity with asyncio.wait_for raising TimeoutError, an
// OSError subclass caught by the fail-closed branch).
func (g *EgressGuard) resolveWithTimeout(ctx context.Context, host string) ([]string, error) {
	ctx2, cancel := context.WithTimeout(ctx, g.timeout)
	defer cancel()
	type result struct {
		addrs []string
		err   error
	}
	ch := make(chan result, 1)
	go func() {
		addrs, err := g.resolver(ctx2, host)
		ch <- result{addrs, err}
	}()
	select {
	case <-ctx2.Done():
		return nil, ctx2.Err()
	case r := <-ch:
		return r.addrs, r.err
	}
}

// resolveCached ports egress._resolve_cached: TTL-bounded per-host cache over
// the timeout-bounded resolver. Used only by the webhook path.
func (g *EgressGuard) resolveCached(ctx context.Context, host string) ([]string, error) {
	now := g.now()
	g.mu.Lock()
	if c, ok := g.cache[host]; ok && (now-c.at) < g.ttlS {
		addrs := c.addrs
		g.mu.Unlock()
		return addrs, nil
	}
	g.mu.Unlock()

	addrs, err := g.resolveWithTimeout(ctx, host)
	if err != nil {
		return nil, err
	}
	g.mu.Lock()
	g.cache[host] = egressCacheEntry{at: now, addrs: addrs}
	g.mu.Unlock()
	return addrs, nil
}

// ── public guard surface ────────────────────────────────────────────────────

// AssertWebhookTargetAllowed ports egress.assert_webhook_target_allowed: raise
// EgressBlockedError if the webhook URL host resolves to a cloud-metadata IP.
// Private/internal hosts ARE allowed. A resolution failure fails closed.
func (g *EgressGuard) AssertWebhookTargetAllowed(ctx context.Context, rawURL string) error {
	host := hostFromURL(rawURL)
	if host == "" {
		return nil
	}
	addresses, err := g.addressesFor(ctx, host, true, fmt.Sprintf("webhook target %q could not be resolved", rawURL))
	if err != nil {
		return err
	}
	for _, addr := range addresses {
		ip := net.ParseIP(addr)
		if ip == nil {
			continue
		}
		if decoded := decodeEmbeddedIPv4(ip); decoded != nil {
			ip = decoded
		}
		if isMetadataIP(ip) {
			return &EgressBlockedError{
				Msg: fmt.Sprintf("webhook target %q resolves to a blocked metadata address", rawURL)}
		}
	}
	return nil
}

// AssertIPAllowed ports egress.assert_ip_allowed: validate an ALREADY-RESOLVED
// literal peer IP with no DNS lookup (connector post-connect / M3 rebinding
// mitigation). Metadata is always blocked; private ranges only when
// blockPrivate. A non-parseable input is a caller bug (returns a plain error).
func AssertIPAllowed(ipStr string, blockPrivate bool) error {
	trimmed := strings.Trim(strings.TrimSpace(ipStr), "[]")
	ip := net.ParseIP(trimmed)
	if ip == nil {
		return fmt.Errorf("invalid IP address: %q", ipStr)
	}
	return checkResolvedIP(ip, blockPrivate,
		fmt.Sprintf("connector peer %q is a blocked metadata address", ipStr),
		fmt.Sprintf("connector peer %q is a blocked internal address", ipStr))
}

// AssertConnectorTargetAllowed ports egress.assert_connector_target_allowed:
// validate a connector target host (literal IP or DNS name). Every resolved
// address is checked; resolution failure fails closed.
func (g *EgressGuard) AssertConnectorTargetAllowed(ctx context.Context, host string, blockPrivate bool) error {
	h := strings.Trim(strings.TrimSpace(host), "[]")
	addresses, err := g.addressesFor(ctx, h, false,
		fmt.Sprintf("could not resolve connector host %q", host))
	if err != nil {
		return err
	}
	for _, addr := range addresses {
		ip := net.ParseIP(addr)
		if ip == nil {
			continue
		}
		if err := checkResolvedIP(ip, blockPrivate,
			fmt.Sprintf("connector target %q resolves to a blocked metadata address", host),
			fmt.Sprintf("connector target %q resolves to a blocked internal address", host)); err != nil {
			return err
		}
	}
	return nil
}

// addressesFor returns the IP strings for a host: the literal itself when host
// is an IP, else the resolved set. cached selects the webhook TTL cache; a
// resolution error or an empty result returns *EgressBlockedError{notResolved}.
func (g *EgressGuard) addressesFor(ctx context.Context, host string, cached bool, notResolved string) ([]string, error) {
	if literal := net.ParseIP(host); literal != nil {
		return []string{literal.String()}, nil
	}
	var (
		resolved []string
		err      error
	)
	if cached {
		resolved, err = g.resolveCached(ctx, host)
	} else {
		resolved, err = g.resolveWithTimeout(ctx, host)
	}
	if err != nil || len(resolved) == 0 {
		return nil, &EgressBlockedError{Msg: notResolved}
	}
	return resolved, nil
}

// AssertSessionEgressAllowed ports egress.assert_session_egress_allowed: derive
// the outbound host from a connector type + config and egress-guard it. This is
// the single chokepoint enforcing security.block_private_connector_targets.
func (g *EgressGuard) AssertSessionEgressAllowed(
	ctx context.Context, connectorType string, connectorConfig map[string]any, blockPrivate bool,
) error {
	var targetHost string
	switch connectorType {
	case "ssh", "telnet":
		if connectorType == "ssh" && connectorConfig["client_key_path"] != nil { // pragma: allowlist secret
			return &EgressBlockedError{Msg: "ssh connector_config.client_key_path is not supported"}
		}
		if h, ok := connectorConfig["host"].(string); ok {
			targetHost = h
		} else if connectorConfig["host"] != nil {
			targetHost = fmt.Sprintf("%v", connectorConfig["host"])
		}
	case "websocket":
		raw, present := connectorConfig["url"]
		if !present || raw == nil {
			return &EgressBlockedError{Msg: "websocket connector requires connector_config.url"}
		}
		parsed, perr := url.Parse(fmt.Sprintf("%v", raw))
		if perr != nil || (parsed.Scheme != "ws" && parsed.Scheme != "wss") {
			return &EgressBlockedError{Msg: "websocket connector_config.url scheme must be ws or wss"}
		}
		targetHost = parsed.Hostname()
		if targetHost == "" {
			return &EgressBlockedError{Msg: "websocket connector_config.url must include a host"}
		}
	}
	if targetHost == "" {
		return nil
	}
	return g.AssertConnectorTargetAllowed(ctx, targetHost, blockPrivate)
}

// hostFromURL parses rawURL and returns its (bracket-stripped) hostname, or ""
// when the URL is malformed or hostless — matching urlparse(url).hostname.
func hostFromURL(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	return strings.Trim(strings.TrimSpace(parsed.Hostname()), "[]")
}
