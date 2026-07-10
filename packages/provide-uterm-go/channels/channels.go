//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package channels implements generic typed-channel negotiation over the
// inline control channel. Port of provide.uterm.channels.
package channels

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// Hello carries client-advertised typed-channel versions.
type Hello struct {
	Channels map[string]int
}

// Negotiated tracks per-connection typed-channel grants and sequence
// counters.
type Negotiated struct {
	supported      map[string]int
	defaultChannel string
	hasDefault     bool
	granted        map[string]int
	seq            map[string]int
}

// NewNegotiated builds a Negotiated set from the supported channel→version
// map. defaultChannel selects the channel used when method calls pass "";
// pass "" for no default.
func NewNegotiated(supported map[string]int, defaultChannel string) (*Negotiated, error) {
	normalized, err := normalizeSupported(supported)
	if err != nil {
		return nil, err
	}
	if defaultChannel != "" {
		if _, ok := normalized[defaultChannel]; !ok {
			return nil, fmt.Errorf("default channel is not supported: %q", defaultChannel)
		}
	}
	return &Negotiated{
		supported:      normalized,
		defaultChannel: defaultChannel,
		hasDefault:     defaultChannel != "",
		granted:        map[string]int{},
		seq:            map[string]int{},
	}, nil
}

// Granted returns a copy of the currently granted channels.
func (n *Negotiated) Granted() map[string]int {
	out := make(map[string]int, len(n.granted))
	for k, v := range n.granted {
		out[k] = v
	}
	return out
}

// IsNegotiated reports whether channel is negotiated; "" selects the
// configured default channel.
func (n *Negotiated) IsNegotiated(channel string) (bool, error) {
	selected, err := n.selectChannel(channel)
	if err != nil {
		return false, err
	}
	_, ok := n.granted[selected]
	return ok, nil
}

// HandleHello negotiates channel versions and returns a hello_ack payload.
// ackFields are merged into the ack; "type" and "channels" are reserved.
func (n *Negotiated) HandleHello(hello Hello, ackFields map[string]any) (map[string]any, error) {
	reserved := make([]string, 0, 2)
	for key := range ackFields {
		if key == "type" || key == "channels" {
			reserved = append(reserved, key)
		}
	}
	if len(reserved) > 0 {
		sort.Strings(reserved)
		return nil, fmt.Errorf("reserved hello_ack field: %s", reserved[0])
	}
	n.granted = negotiate(n.supported, hello.Channels)
	ack := map[string]any{"type": "hello_ack", "channels": n.Granted()}
	for key, value := range ackFields {
		ack[key] = value
	}
	return ack, nil
}

// NextSeq increments and returns the sequence number for channel ("" selects
// the default channel).
func (n *Negotiated) NextSeq(channel string) (int, error) {
	selected, err := n.selectChannel(channel)
	if err != nil {
		return 0, err
	}
	n.seq[selected]++
	return n.seq[selected], nil
}

// ExportGrants returns a serializable granted-channel map.
func (n *Negotiated) ExportGrants() map[string]int {
	return n.Granted()
}

// RestoreGrants restores persisted grants and resets sequence counters for a
// fresh channel instance.
func (n *Negotiated) RestoreGrants(grants map[string]any) error {
	coerced, err := coerceChannelMap(grants)
	if err != nil {
		return err
	}
	n.granted = negotiate(n.supported, coerced)
	n.seq = map[string]int{}
	return nil
}

func (n *Negotiated) selectChannel(channel string) (string, error) {
	if channel != "" {
		return channel, nil
	}
	if !n.hasDefault {
		return "", errors.New("channel is required when no default_channel is configured")
	}
	return n.defaultChannel, nil
}

// ParseChannelHello parses a framed hello payload, returning nil when raw is
// not a channel hello (mirroring the Python None return).
func ParseChannelHello(raw string) *Hello {
	if raw == "" || !controlchannel.IsControlFrame(raw) {
		return nil
	}
	for _, chunk := range decodeFrames(raw) {
		ctrl, ok := chunk.(controlchannel.ControlChunk)
		if !ok || ctrl.Control["type"] != "hello" {
			continue
		}
		channelsRaw, _ := ctrl.Control["channels"].(map[string]any)
		coerced, err := coerceChannelMap(channelsRaw)
		if err != nil {
			return nil
		}
		return &Hello{Channels: coerced}
	}
	return nil
}

// decodeFrames decodes raw into chunks, returning nil on any protocol error
// (mirroring the Python bare-except around feed()+finish()).
func decodeFrames(raw string) []controlchannel.Chunk {
	decoder := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	chunks, err := decoder.Feed(raw)
	if err != nil {
		return nil
	}
	fin, err := decoder.Finish()
	if err != nil {
		return nil
	}
	return append(chunks, fin...)
}

func normalizeSupported(supported map[string]int) (map[string]int, error) {
	normalized := make(map[string]int, len(supported))
	for name, version := range supported {
		if name == "" {
			return nil, errors.New("channel names must be non-empty strings")
		}
		normalized[name] = version
	}
	if len(normalized) == 0 {
		return nil, errors.New("at least one supported channel is required")
	}
	return normalized, nil
}

// coerceChannelMap validates a decoded-JSON channel map. Versions must be
// integers: JSON numbers arrive as float64, so integral floats are accepted
// and anything else (bool, string, fractional) is rejected, matching the
// Python isinstance(version, int)-and-not-bool check.
func coerceChannelMap(value map[string]any) (map[string]int, error) {
	if value == nil {
		return nil, errors.New("channels must be a mapping")
	}
	channels := make(map[string]int, len(value))
	for name, version := range value {
		if name == "" {
			return nil, errors.New("channel names must be non-empty strings")
		}
		switch v := version.(type) {
		case int:
			channels[name] = v
		case float64:
			if v != math.Trunc(v) || math.IsInf(v, 0) {
				return nil, errors.New("channel versions must be integers")
			}
			channels[name] = int(v)
		case json.Number:
			// controlchannel decodes wire numbers as json.Number to preserve
			// the int/float distinction; a channel version must be an integer.
			n, err := v.Int64()
			if err != nil {
				return nil, errors.New("channel versions must be integers")
			}
			channels[name] = int(n)
		default:
			return nil, errors.New("channel versions must be integers")
		}
	}
	return channels, nil
}

func negotiate(supported, requested map[string]int) map[string]int {
	granted := map[string]int{}
	for name, version := range requested {
		supportedVersion, ok := supported[name]
		if ok && version > 0 {
			granted[name] = min(version, supportedVersion)
		}
	}
	return granted
}
