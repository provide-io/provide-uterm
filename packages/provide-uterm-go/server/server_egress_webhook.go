//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net"
	"net/url"
	"strings"
)

// metadataGoogleInternal is the GCE metadata *hostname*. It is refused by name
// as well as by address: the name is the form an attacker actually supplies, and
// on GCE it resolves to 169.254.169.254 — but a hostile resolver, an /etc/hosts
// entry or a split-horizon zone can point it anywhere, so the name is not
// trusted to resolve to something the address check would catch.
const metadataGoogleInternal = "metadata.google.internal"

// CheckWebhookDestination is the egress guard for a *session-registered*
// webhook destination — a URL supplied by whoever can mutate a session, driving
// a request from the server's network position. It implements §1–§3 and §5 of
// conformance/EGRESS_GUARD.md and is the port of webhooks.validate_webhook_url
// (registration) / webhooks._delivery_url_allowed (delivery), which share one
// address classifier in the reference and share one here.
//
// It is deliberately NOT AssertWebhookTargetAllowed. That method guards
// *operator-configured* outbound URLs (the PAM relay, the discovery announcer,
// the webhook IdP) where private and loopback targets are legitimate and only
// cloud metadata is refused. This method guards *caller-supplied* URLs, where
// the whole private/reserved space is refused and loopback is refused unless
// explicitly permitted. Two different trust levels, two different refusal sets;
// collapsing them would either break every single-box relay deployment or
// hand session mutators the internal network.
//
// allowLoopback is the §3 effective permission, computed once where the server
// is built from config — never re-derived here.
//
// The reported loopback flag says "at least one address this destination
// resolves to is loopback". Delivery needs it for the §4 tunnel-share check,
// which cannot be answered at config-load time. It is reported even alongside
// an error so a caller never has to re-resolve to find out.
//
// Every refusal is an *EgressBlockedError. Resolution failure and an empty
// answer are refusals, not passes: a name that cannot be checked has not been
// cleared.
func (g *EgressGuard) CheckWebhookDestination(
	ctx context.Context, rawURL string, allowLoopback bool,
) (loopback bool, err error) {
	parsed, perr := url.Parse(rawURL)
	if perr != nil {
		return false, &EgressBlockedError{Msg: "webhook url is invalid"}
	}
	// http(s) only. A ws://, file:// or gopher:// destination is not something
	// the deliverer can POST to, and the exotic schemes are classic SSRF
	// primitives (file:// reads local disk, gopher:// forges arbitrary TCP).
	switch strings.ToLower(parsed.Scheme) {
	case "http", "https":
	default:
		return false, &EgressBlockedError{Msg: "webhook url must use http or https"}
	}
	host := strings.Trim(strings.TrimSpace(parsed.Hostname()), "[]")
	if host == "" {
		return false, &EgressBlockedError{Msg: "webhook url must include a host"}
	}
	// Trailing dots make "metadata.google.internal." a distinct string that
	// resolves identically, so normalise before any name comparison.
	hostname := strings.ToLower(strings.TrimRight(host, "."))

	if hostname == metadataGoogleInternal {
		return false, &EgressBlockedError{Msg: "webhook url host is not allowed"}
	}
	// "localhost" and any "*.localhost" are loopback by definition (RFC 6761),
	// so they are answered from the name without consulting a resolver — which
	// also means a resolver that maps localhost elsewhere cannot launder a
	// destination through it.
	if hostname == "localhost" || strings.HasSuffix(hostname, ".localhost") {
		if allowLoopback {
			return true, nil
		}
		return true, &EgressBlockedError{Msg: "webhook url host is not allowed"}
	}

	addresses, err := g.addressesFor(ctx, hostname, true, "webhook url host could not be resolved")
	if err != nil {
		return false, err
	}
	for _, addr := range addresses {
		ip := net.ParseIP(addr)
		if ip == nil {
			// A resolver that answered with something unparseable has not
			// cleared this destination either.
			return loopback, &EgressBlockedError{Msg: "webhook url host is not allowed"}
		}
		isLoopback, allowed := webhookAddressAllowed(ip, allowLoopback)
		loopback = loopback || isLoopback
		if !allowed {
			return loopback, &EgressBlockedError{Msg: "webhook url host is not allowed"}
		}
	}
	return loopback, nil
}

// webhookAddressAllowed classifies one resolved address for a webhook
// destination. Port of webhooks._address_allowed, in its exact order, over the
// canonical CIDR union in server_egress.go (blockedPrivateV4 /
// blockedPrivateV6 / metadataIPs) rather than a second copy of it.
//
// Returns (isLoopback, allowed). The loopback bit is reported separately
// because §4 needs it even when the address is allowed.
func webhookAddressAllowed(ip net.IP, allowLoopback bool) (isLoopback, allowed bool) {
	// An IPv6 wrapper can carry an IPv4 payload (64:ff9b::169.254.169.254
	// reaches the v4 metadata service through NAT64), so decode before any
	// classification. Unlike the Python reference, which leans on CPython's
	// ipaddress classifiers to reject every wrapped form, this port has no such
	// backstop and must decode explicitly.
	if decoded := decodeEmbeddedIPv4(ip); decoded != nil {
		ip = decoded
	}
	// §1: cloud metadata is refused unconditionally. There is deliberately no
	// key that re-opens it.
	if isMetadataIP(ip) {
		return false, false
	}
	// §2: loopback is the one conditional case. Binding to 127.0.0.1 is itself
	// an access control — services listen there so the network cannot reach
	// them, and skip authentication on that basis — so reaching it via the
	// server is a privilege escalation unless the deployment says otherwise.
	if ip.IsLoopback() {
		return true, allowLoopback
	}
	// §1: everything else private / link-local / multicast / unspecified /
	// IANA-reserved is refused with no knob.
	if isBlockedPrivate(ip) {
		return false, false
	}
	return false, true
}
