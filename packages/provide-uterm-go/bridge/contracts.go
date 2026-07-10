//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package bridge is a Go port of the worker-side terminal-bridge layer of
// provide-uterm. It brings together three Python source modules:
//
//   - provide.uterm.bridge.contracts — the shared protocol-version range
//     negotiation (this file).
//   - provide.uterm.bridge.base (HijackableMixin) — the checkpoint
//     hijackability primitives a worker embeds (hijackable.go).
//   - provide.uterm.bridge.coordinator (HijackCoordinator) — the single-writer
//     hijack-lease arbitration state machine (coordinator.go).
//   - provide.uterm.server.bridge.worker_link (TermBridge) — the worker-side
//     WebSocket client that connects to the hub and speaks the inline DLE/STX
//     control channel (worker_link.go).
//
// Wire framing is delegated to the controlchannel package; frame shapes come
// from the frames package.
package bridge

import "fmt"

// Protocol-version range carried in the hello-frame handshake.
//
// Peers advertise {"min": MIN, "max": MAX, "preferred": PREFERRED} in their
// hello frame. The server intersects the client range against its own and
// picks the highest mutually-supported version, or closes the WebSocket with
// code 1002 + an error frame if there is no overlap.
//
// Lockstep is preserved while only one version exists (min == max == 1). When
// a new protocol version lands, bump MaxProtocolVersion first, leave
// MinProtocolVersion at the oldest still-supported version, and set
// PreferredProtocolVersion to whatever the server should actively pick during
// negotiation. Port of provide.uterm.bridge.contracts.
const (
	// MinProtocolVersion is the oldest protocol version this build supports.
	MinProtocolVersion = 1
	// MaxProtocolVersion is the newest protocol version this build supports.
	MaxProtocolVersion = 1
	// PreferredProtocolVersion is the version the server actively picks.
	PreferredProtocolVersion = 1
	// CurrentProtocolVersion is a backward-compatible alias for "the current
	// version" (typically for stamping outbound frames). New code should
	// reference the range fields above.
	CurrentProtocolVersion = PreferredProtocolVersion
)

// ProtocolMismatchError is returned when a client's advertised protocol range
// does not overlap the server's [MinProtocolVersion, MaxProtocolVersion]. It
// carries the four bounds that the Python handler stamps onto the
// reason="protocol_mismatch" error frame so a client can surface a useful
// disconnect message.
type ProtocolMismatchError struct {
	ClientMin int
	ClientMax int
	ServerMin int
	ServerMax int
}

// Error implements the error interface.
func (e *ProtocolMismatchError) Error() string {
	return fmt.Sprintf(
		"protocol_mismatch: client=[%d,%d] server=[%d,%d]",
		e.ClientMin, e.ClientMax, e.ServerMin, e.ServerMax,
	)
}

// NegotiateProtocolVersion returns the version both sides should use and
// ok=true, or ok=false on no overlap.
//
// The chosen version is the highest of [ServerMin..ServerMax] intersect
// [clientMin..clientMax]. ok=false means the handshake must fail and the
// caller should close 1002. Port of negotiate_protocol_version.
func NegotiateProtocolVersion(clientMin, clientMax int) (int, bool) {
	lo := max(clientMin, MinProtocolVersion)
	hi := min(clientMax, MaxProtocolVersion)
	if lo > hi {
		return 0, false
	}
	return hi, true
}

// Negotiate intersects the client range against the server range and returns
// the selected version, or a typed *ProtocolMismatchError carrying all four
// bounds when the ranges do not overlap. It is the error-returning companion
// to NegotiateProtocolVersion.
func Negotiate(clientMin, clientMax int) (int, error) {
	selected, ok := NegotiateProtocolVersion(clientMin, clientMax)
	if !ok {
		return 0, &ProtocolMismatchError{
			ClientMin: clientMin,
			ClientMax: clientMax,
			ServerMin: MinProtocolVersion,
			ServerMax: MaxProtocolVersion,
		}
	}
	return selected, nil
}

// ParseClientRange extracts the advertised [min, max] protocol range from a
// decoded worker_hello message, mirroring the server handler's parsing:
//
//   - a "protocol" object {min, max, preferred} wins; missing min/max default
//     to MinProtocolVersion / MaxProtocolVersion and are floored at 1;
//   - else a legacy "protocol_version" int is treated as min == max == v
//     (floored at 1);
//   - else the range defaults to {1, 1} (pre-negotiation workers).
//
// Port of the parsing block in _handle_worker_hello.
func ParseClientRange(msg map[string]any) (clientMin, clientMax int) {
	if proto, ok := msg["protocol"].(map[string]any); ok {
		clientMin = safeInt(proto["min"], MinProtocolVersion, 1)
		clientMax = safeInt(proto["max"], MaxProtocolVersion, 1)
		return clientMin, clientMax
	}
	if raw, ok := msg["protocol_version"]; ok {
		v := safeInt(raw, 0, 0)
		if v < 1 {
			v = 1
		}
		return v, v
	}
	return 1, 1
}

// NegotiateFromHello parses a decoded worker_hello message and negotiates the
// protocol version, returning the selected version or a *ProtocolMismatchError.
// It is the whole server-side negotiation step (ParseClientRange + Negotiate)
// in one call.
func NegotiateFromHello(msg map[string]any) (int, error) {
	clientMin, clientMax := ParseClientRange(msg)
	return Negotiate(clientMin, clientMax)
}
