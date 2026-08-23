//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package controlchannel implements the inline DLE/STX control framing used to
// mix terminal data and JSON control messages in one WebSocket stream.
//
// This is a direct port of provide.uterm.control_channel (Python). Control
// frame headers store the UTF-8 byte length of the JSON payload. Buffers are
// Go strings (UTF-8), so the declared payload byte length maps directly onto
// string byte offsets; a declared length that splits a multi-byte rune is a
// protocol error, exactly as the Python decoder treats a length that splits a
// Unicode code point.
package controlchannel

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"
)

const (
	// DLE and STX are the frame magic bytes.
	DLE = "\x10"
	STX = "\x02"

	headerBytes            = 11 // DLE STX + 8 hex digits + ':'
	maxControlPayloadBytes = 1_048_576
	defaultMaxBufferBytes  = 10_485_760
	defaultMaxFrameDepth   = 32
	dleByte                = 0x10
	stxByte                = 0x02
)

// ProtocolError is raised when an inline control frame is malformed.
type ProtocolError struct {
	msg string
}

func (e *ProtocolError) Error() string { return e.msg }

func protocolErrorf(format string, args ...any) *ProtocolError {
	return &ProtocolError{msg: fmt.Sprintf(format, args...)}
}

// Chunk is a decoded element of the inline stream: either terminal data or a
// control payload.
type Chunk interface {
	// Kind returns "data" or "control".
	Kind() string
}

// DataChunk is decoded terminal data from the inline stream.
type DataChunk struct {
	Data string
}

// Kind implements Chunk.
func (DataChunk) Kind() string { return "data" }

// ControlChunk is a decoded control payload from the inline stream.
type ControlChunk struct {
	Control map[string]any
}

// Kind implements Chunk.
func (ControlChunk) Kind() string { return "control" }

// EncodeTerminalData encodes terminal data for the inline stream by escaping
// every DLE byte.
func EncodeTerminalData(data string) string {
	return strings.ReplaceAll(data, DLE, DLE+DLE)
}

// EncodeControlFrame encodes a control payload for the inline stream.
//
// The JSON serialization is compact (no spaces) and does not escape non-ASCII
// characters or HTML metacharacters, matching the Python encoder.
func EncodeControlFrame(payload map[string]any) (string, error) {
	serialized, err := marshalCompact(payload)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%s%s%08x:%s", DLE, STX, len(serialized), serialized), nil
}

func marshalCompact(payload map[string]any) (string, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(payload); err != nil {
		return "", err
	}
	// json.Encoder appends a trailing newline; the wire format must not carry it.
	return strings.TrimSuffix(buf.String(), "\n"), nil
}

func parseHex32(s string) (int, bool) {
	v := 0
	for i := 0; i < len(s); i++ {
		c := s[i]
		var d int
		switch {
		case c >= '0' && c <= '9':
			d = int(c - '0')
		case c >= 'a' && c <= 'f':
			d = int(c-'a') + 10
		case c >= 'A' && c <= 'F':
			d = int(c-'A') + 10
		default:
			return 0, false
		}
		v = v<<4 | d
	}
	return v, true
}

// utf8PayloadEnd returns the byte index ending a payload of payloadBytes UTF-8
// bytes starting at start, or -1 when buf does not yet contain that many
// bytes. It returns a ProtocolError when the declared byte length splits a
// multi-byte rune (which appending more data cannot fix).
func utf8PayloadEnd(buf string, start, payloadBytes int) (int, error) {
	end := start + payloadBytes
	if end > len(buf) {
		return -1, nil
	}
	// Go strings from the WebSocket layer are valid UTF-8, so end == len(buf)
	// is always a rune boundary; otherwise the byte at end must not be a
	// UTF-8 continuation byte.
	if end < len(buf) && !utf8.RuneStart(buf[end]) {
		return -1, protocolErrorf("invalid control payload length")
	}
	return end, nil
}

// IsControlFrame reports whether message is a full control-framed payload.
//
// The check is structural only: it validates the magic bytes, length header
// syntax, and that the declared UTF-8 payload bytes are fully present.
func IsControlFrame(message string) bool {
	if len(message) < headerBytes {
		return false
	}
	if !strings.HasPrefix(message, DLE+STX) {
		return false
	}
	if message[10] != ':' {
		return false
	}
	lengthHex := message[2:10]
	payloadBytes, ok := parseHex32(lengthHex)
	if !ok {
		return false
	}
	if fmt.Sprintf("%08x", payloadBytes) != lengthHex {
		return false
	}
	if payloadBytes > maxControlPayloadBytes {
		return false
	}
	payloadEnd, err := utf8PayloadEnd(message, headerBytes, payloadBytes)
	if err != nil {
		return false
	}
	return payloadEnd != -1 && payloadEnd == len(message)
}

// checkJSONDepth returns a ProtocolError if value nests deeper than maxDepth.
// Maps and slices each add one level; primitive leaves count as depth 0. The
// walk is iterative so a pathological payload cannot exhaust the stack.
func checkJSONDepth(value any, maxDepth int) error {
	type item struct {
		node  any
		depth int
	}
	stack := []item{{value, 1}}
	for len(stack) > 0 {
		it := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		if it.depth > maxDepth {
			return protocolErrorf("control payload nests deeper than %d", maxDepth)
		}
		switch node := it.node.(type) {
		case map[string]any:
			for _, child := range node {
				switch child.(type) {
				case map[string]any, []any:
					stack = append(stack, item{child, it.depth + 1})
				}
			}
		case []any:
			for _, child := range node {
				switch child.(type) {
				case map[string]any, []any:
					stack = append(stack, item{child, it.depth + 1})
				}
			}
		}
	}
	return nil
}

// DecoderOptions configure a Decoder. Zero values select the defaults.
type DecoderOptions struct {
	// MaxControlPayloadBytes bounds a single control frame payload. Values
	// above the protocol maximum (1 MiB) are still capped at 1 MiB.
	MaxControlPayloadBytes int
	// MaxBufferBytes bounds the total undecoded buffer.
	MaxBufferBytes int
	// MaxFrameDepth bounds JSON nesting inside a control frame.
	MaxFrameDepth int
	// OnError, when set, is invoked with "control_frame_protocol_error"
	// whenever the decoder raises a protocol error.
	OnError func(string)
}

// Decoder incrementally decodes the inline DLE/STX control-frame stream.
type Decoder struct {
	maxControlPayloadBytes int
	maxBufferBytes         int
	maxFrameDepth          int
	onError                func(string)
	buffered               string
}

// NewDecoder creates a Decoder with the given options.
func NewDecoder(opts DecoderOptions) *Decoder {
	d := &Decoder{
		maxControlPayloadBytes: opts.MaxControlPayloadBytes,
		maxBufferBytes:         opts.MaxBufferBytes,
		maxFrameDepth:          opts.MaxFrameDepth,
		onError:                opts.OnError,
	}
	if d.maxControlPayloadBytes < 1 {
		d.maxControlPayloadBytes = maxControlPayloadBytes
	}
	if d.maxBufferBytes < 1 {
		d.maxBufferBytes = defaultMaxBufferBytes
	}
	if d.maxFrameDepth < 1 {
		d.maxFrameDepth = defaultMaxFrameDepth
	}
	return d
}

func (d *Decoder) reportError(format string, args ...any) error {
	if d.onError != nil {
		d.onError("control_frame_protocol_error")
	}
	return protocolErrorf(format, args...)
}

func (d *Decoder) reset() {
	d.buffered = ""
}

// Feed decodes all complete events from chunk and buffers the rest.
func (d *Decoder) Feed(chunk string) ([]Chunk, error) {
	if len(d.buffered)+len(chunk) > d.maxBufferBytes {
		total := len(d.buffered) + len(chunk)
		d.reset()
		return nil, d.reportError("control frame buffer overflow: %d > %d", total, d.maxBufferBytes)
	}
	d.buffered += chunk
	events, err := d.drain(false)
	if err != nil {
		d.reset()
		return nil, err
	}
	return events, nil
}

// Finish decodes any remaining buffered data and rejects truncated control
// frames.
func (d *Decoder) Finish() ([]Chunk, error) {
	events, err := d.drain(true)
	if err != nil {
		d.reset()
		return nil, err
	}
	// A final drain either consumes the whole buffer or errors: every
	// incomplete construct (lone DLE, short header, short payload) raises
	// "truncated control frame" inside drain, so no leftover check is needed.
	return events, nil
}

func (d *Decoder) parseFramePayload(payloadRaw string) (map[string]any, error) {
	dec := json.NewDecoder(strings.NewReader(payloadRaw))
	// Preserve each JSON number's source text as json.Number rather than
	// collapsing to float64. Python's json.loads keeps the int/float
	// distinction ("1" -> int, "1.0" -> float), which is load-bearing for
	// identity-frame HMAC signatures: a verifier must canonicalize the
	// received claims exactly as the producer did. float64 loses that
	// distinction (both decode to 1.0), so an integer claim would re-sign as
	// "1.0" and fail verification against Python's "1".
	dec.UseNumber()
	var payload any
	if err := dec.Decode(&payload); err != nil {
		return nil, d.reportError("invalid control json")
	}
	// Reject trailing garbage after the JSON document, matching Python's
	// strict json.loads behavior.
	if dec.More() {
		return nil, d.reportError("invalid control json")
	}
	obj, ok := payload.(map[string]any)
	if !ok {
		return nil, d.reportError("control payload must be an object")
	}
	if err := checkJSONDepth(obj, d.maxFrameDepth); err != nil {
		return nil, d.reportError("%s", err.Error())
	}
	return obj, nil
}

// tryParseFrame parses a control frame at buf[idx]. It returns the chunk and
// the frame end offset, or done=false when the frame is not yet complete
// (only valid when final is false).
func (d *Decoder) tryParseFrame(buf string, idx int, final bool) (chunk ControlChunk, frameEnd int, done bool, err error) {
	if len(buf)-idx < headerBytes {
		if final {
			return ControlChunk{}, 0, false, d.reportError("truncated control frame")
		}
		return ControlChunk{}, 0, false, nil
	}
	lengthHex := buf[idx+2 : idx+10]
	separator := buf[idx+10]
	payloadBytes, hexOK := parseHex32(lengthHex)
	// The canonical comparison, not just parseHex32's digit test: parseHex32
	// accepts A-F, so without this the decoder reads "0000001F" as a frame
	// while IsControlFrame -- which has always compared against %08x -- reads
	// the same bytes as terminal data. IsControlFrame is what decides whether
	// a message is framed at all, so the two must agree. Pinned as
	// CCF-REG-0006 in the shared fuzz corpus.
	if separator != ':' || !hexOK || fmt.Sprintf("%08x", payloadBytes) != lengthHex {
		return ControlChunk{}, 0, false, d.reportError("invalid control header")
	}
	// Bounding check: oversized frames are rejected before allocation.
	if payloadBytes > maxControlPayloadBytes || payloadBytes > d.maxControlPayloadBytes {
		return ControlChunk{}, 0, false, d.reportError("control payload too large")
	}
	payloadStart := idx + headerBytes
	end, perr := utf8PayloadEnd(buf, payloadStart, payloadBytes)
	if perr != nil {
		return ControlChunk{}, 0, false, d.reportError("%s", perr.Error())
	}
	if end == -1 {
		if final {
			return ControlChunk{}, 0, false, d.reportError("truncated control frame")
		}
		return ControlChunk{}, 0, false, nil
	}
	payload, err := d.parseFramePayload(buf[payloadStart:end])
	if err != nil {
		return ControlChunk{}, 0, false, err
	}
	return ControlChunk{Control: payload}, end, true, nil
}

func (d *Decoder) drain(final bool) ([]Chunk, error) {
	var events []Chunk
	buf := d.buffered
	bufLen := len(buf)
	idx := 0
	var dataParts strings.Builder
	dataStart := 0

	emitData := func(upTo int) {
		if dataStart < upTo {
			dataParts.WriteString(buf[dataStart:upTo])
		}
		if dataParts.Len() > 0 {
			events = append(events, DataChunk{Data: dataParts.String()})
			dataParts.Reset()
		}
	}

	for idx < bufLen {
		if buf[idx] != dleByte {
			idx++
			continue
		}
		if idx+1 >= bufLen {
			if final {
				return nil, d.reportError("truncated control frame")
			}
			break
		}
		next := buf[idx+1]
		if next == dleByte {
			// Escaped DLE: save the data slice before it, add a literal DLE.
			if dataStart < idx {
				dataParts.WriteString(buf[dataStart:idx])
			}
			dataParts.WriteString(DLE)
			idx += 2
			dataStart = idx
			continue
		}
		if next != stxByte {
			return nil, d.reportError("invalid control prefix")
		}

		emitData(idx)
		dataStart = idx

		chunk, frameEnd, done, err := d.tryParseFrame(buf, idx, final)
		if err != nil {
			return nil, err
		}
		if !done {
			break
		}
		idx = frameEnd
		dataStart = idx
		events = append(events, chunk)
	}

	// Flush the unconsumed buffer tail and any trailing plain data.
	if idx > 0 {
		d.buffered = buf[idx:]
	}
	emitData(idx)
	return events, nil
}
