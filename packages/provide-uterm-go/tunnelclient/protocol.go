//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package tunnelclient is the Go port of the Python provide.uterm.tunnel
// client stack (protocol, WebSocket client, PTY capture, HTTP proxy and
// request interception). It lets a Go `uterm` binary talk to a Python tunnel
// server byte-for-byte over the framed binary WebSocket protocol.
//
// The wire codec here (EncodeFrame/DecodeFrame/EncodeControl) is a faithful
// port of provide/uterm/tunnel/protocol.py and is differential-tested against
// a committed Python-generated golden corpus (see protocol_test.go).
package tunnelclient

import (
	"encoding/json"
	"errors"
	"fmt"
)

// Channel identifiers, matching provide/uterm/tunnel/protocol.py exactly.
const (
	// ChannelControl carries JSON control messages (open/resize/state).
	ChannelControl byte = 0x00
	// ChannelData carries raw terminal bytes (share).
	ChannelData byte = 0x01
	// ChannelTCP carries raw TCP relay bytes (tunnel).
	ChannelTCP byte = 0x02
	// ChannelHTTP carries structured HTTP inspection JSON (inspect).
	ChannelHTTP byte = 0x03
)

// Frame flags, matching protocol.py.
const (
	// FlagData is the default flag (no special semantics).
	FlagData byte = 0x00
	// FlagEOF marks the end of a channel's data stream.
	FlagEOF byte = 0x01
)

// ErrFrameTooShort is returned by DecodeFrame when fewer than 2 bytes are given.
var ErrFrameTooShort = errors.New("tunnelclient: frame too short")

// Frame is a decoded tunnel frame: [channel][flags][payload...].
type Frame struct {
	Channel byte
	Flags   byte
	Payload []byte
}

// IsEOF reports whether the EOF flag is set on this frame.
func (f Frame) IsEOF() bool { return f.Flags&FlagEOF != 0 }

// IsControl reports whether this frame is on the control channel.
func (f Frame) IsControl() bool { return f.Channel == ChannelControl }

// EncodeFrame encodes a tunnel frame: [channel][flags][payload]. It never
// copies payload's tail — the returned slice is a fresh buffer, so the caller
// may reuse payload afterward.
func EncodeFrame(channel byte, payload []byte, flags byte) []byte {
	out := make([]byte, 2+len(payload))
	out[0] = channel
	out[1] = flags
	copy(out[2:], payload)
	return out
}

// DecodeFrame decodes a tunnel frame from raw bytes. The returned Payload
// aliases data[2:] and must not be retained if data is reused.
func DecodeFrame(data []byte) (Frame, error) {
	if len(data) < 2 {
		return Frame{}, ErrFrameTooShort
	}
	return Frame{Channel: data[0], Flags: data[1], Payload: data[2:]}, nil
}

// EncodeControl encodes a control message as a compact-JSON frame on the
// control channel. It mirrors Python's encode_control (which requires a "type"
// key and uses ("," , ":") separators). Passing an already-marshalled body via
// EncodeControlBytes is preferred where key order must match Python exactly.
func EncodeControl(msg map[string]any) ([]byte, error) {
	if _, ok := msg["type"]; !ok {
		return nil, errors.New("tunnelclient: control message must have a 'type' key")
	}
	payload, err := json.Marshal(msg)
	if err != nil {
		return nil, fmt.Errorf("tunnelclient: marshal control: %w", err)
	}
	return EncodeFrame(ChannelControl, payload, FlagData), nil
}

// EncodeControlBytes wraps an already-serialized JSON body as a control frame.
// The share/tunnel/inspect commands use this with order-preserving structs so
// the emitted bytes are identical to Python's json.dumps(separators=(",",":")).
func EncodeControlBytes(payload []byte) []byte {
	return EncodeFrame(ChannelControl, payload, FlagData)
}

// DecodeControl parses a control payload (JSON bytes) into a map.
func DecodeControl(payload []byte) (map[string]any, error) {
	var obj map[string]any
	if err := json.Unmarshal(payload, &obj); err != nil {
		return nil, fmt.Errorf("tunnelclient: invalid control payload: %w", err)
	}
	return obj, nil
}
