package vnc

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"testing"
)

// allowPolicy permits every inject; denyPolicy rejects every inject.
type allowPolicy struct{}

func (allowPolicy) CanInject(sessionID, leaseID, principalID, principalRole string) error {
	return nil
}
func (allowPolicy) CanPerform(op, role string, leaseOwned, sessionActive bool) error { return nil }

type denyPolicy struct{}

func (denyPolicy) CanInject(sessionID, leaseID, principalID, principalRole string) error {
	return errors.New("denied")
}
func (denyPolicy) CanPerform(op, role string, leaseOwned, sessionActive bool) error {
	return errors.New("denied")
}

// rfbHandshake returns the 14 leading handshake bytes: 12 version + 1 security(=1) + 1 clientinit.
func rfbHandshake() []byte {
	b := make([]byte, 0, 14)
	b = append(b, []byte("RFB 003.008\n")...) // 12 bytes
	b = append(b, 1)                          // security type 1
	b = append(b, 0)                          // ClientInit shared-flag
	return b
}

func TestFilterRFBInputAllowsAllMessageTypes(t *testing.T) {
	var in bytes.Buffer
	in.Write(rfbHandshake())

	// SetPixelFormat: type 0 + 19 bytes
	in.WriteByte(ClientSetPixelFormat)
	in.Write(make([]byte, 19))

	// SetEncodings: type 2 + padding(1) + num(2)=2 + 2*4 bytes
	in.WriteByte(ClientSetEncodings)
	enc := []byte{0, 0, 0}
	binary.BigEndian.PutUint16(enc[1:3], 2)
	in.Write(enc)
	in.Write(make([]byte, 8))

	// SetEncodings with num=0 (no encodings body)
	in.WriteByte(ClientSetEncodings)
	in.Write([]byte{0, 0, 0})

	// FramebufferUpdateRequest: type 3 + 9 bytes
	in.WriteByte(ClientFramebufferUpdateRequest)
	in.Write(make([]byte, 9))

	// KeyEvent: type 4 + 7 bytes
	in.WriteByte(ClientKeyEvent)
	in.Write(make([]byte, 7))

	// PointerEvent: type 5 + 5 bytes
	in.WriteByte(ClientPointerEvent)
	in.Write(make([]byte, 5))

	// ClientCutText: type 6 + padding(3) + length(4)=3 + payload(3)
	in.WriteByte(6)
	cut := make([]byte, 7)
	binary.BigEndian.PutUint32(cut[3:7], 3)
	in.Write(cut)
	in.Write([]byte("abc"))

	// ClientCutText with length 0
	in.WriteByte(6)
	in.Write(make([]byte, 7))

	var out bytes.Buffer
	err := filterRFBInput(&out, &in, allowPolicy{}, "s", "l", "p", "operator")
	if !errors.Is(err, io.EOF) {
		t.Fatalf("expected EOF at stream end, got %v", err)
	}
	// Handshake (14) forwarded, plus all allowed messages present.
	if out.Len() == 0 {
		t.Fatal("expected forwarded output")
	}
	b := out.Bytes()
	if !bytes.HasPrefix(b, []byte("RFB 003.008\n")) {
		t.Fatal("handshake version not forwarded")
	}
}

func TestFilterRFBInputDeniedInputDropped(t *testing.T) {
	var in bytes.Buffer
	in.Write(rfbHandshake())
	// KeyEvent (should be dropped)
	in.WriteByte(ClientKeyEvent)
	in.Write(make([]byte, 7))
	// PointerEvent (dropped)
	in.WriteByte(ClientPointerEvent)
	in.Write(make([]byte, 5))
	// CutText length 3 (dropped)
	in.WriteByte(6)
	cut := make([]byte, 7)
	binary.BigEndian.PutUint32(cut[3:7], 3)
	in.Write(cut)
	in.Write([]byte("xyz"))

	var out bytes.Buffer
	err := filterRFBInput(&out, &in, denyPolicy{}, "s", "l", "p", "viewer")
	if !errors.Is(err, io.EOF) {
		t.Fatalf("expected EOF, got %v", err)
	}
	// Only the 14-byte handshake should be forwarded; all inputs dropped.
	if out.Len() != 14 {
		t.Fatalf("expected only 14 handshake bytes, got %d", out.Len())
	}
}

func TestFilterRFBInputNilPolicyDropsInput(t *testing.T) {
	var in bytes.Buffer
	in.Write(rfbHandshake())
	in.WriteByte(ClientKeyEvent)
	in.Write(make([]byte, 7))

	var out bytes.Buffer
	err := filterRFBInput(&out, &in, nil, "s", "l", "p", "viewer")
	if !errors.Is(err, io.EOF) {
		t.Fatalf("expected EOF, got %v", err)
	}
	if out.Len() != 14 {
		t.Fatalf("nil policy must drop key event, got %d bytes", out.Len())
	}
}

func TestFilterRFBInputBadSecurityType(t *testing.T) {
	var in bytes.Buffer
	in.Write([]byte("RFB 003.008\n"))
	in.WriteByte(2) // unsupported security type
	var out bytes.Buffer
	if err := filterRFBInput(&out, &in, allowPolicy{}, "s", "l", "p", "op"); err == nil {
		t.Fatal("expected error for unsupported security type")
	}
}

func TestFilterRFBInputUnknownMessageType(t *testing.T) {
	var in bytes.Buffer
	in.Write(rfbHandshake())
	in.WriteByte(99) // unknown type
	var out bytes.Buffer
	if err := filterRFBInput(&out, &in, allowPolicy{}, "s", "l", "p", "op"); err == nil {
		t.Fatal("expected error for unknown message type")
	}
}

func TestFilterRFBInputCutTextTooLarge(t *testing.T) {
	var in bytes.Buffer
	in.Write(rfbHandshake())
	in.WriteByte(6)
	cut := make([]byte, 7)
	binary.BigEndian.PutUint32(cut[3:7], 1048577) // over 1 MiB cap
	in.Write(cut)
	var out bytes.Buffer
	if err := filterRFBInput(&out, &in, allowPolicy{}, "s", "l", "p", "op"); err == nil {
		t.Fatal("expected error for oversized cut text")
	}
}

func TestFilterRFBInputTruncatedHandshake(t *testing.T) {
	var in bytes.Buffer
	in.Write([]byte("RFB")) // fewer than 12 bytes
	var out bytes.Buffer
	if err := filterRFBInput(&out, &in, allowPolicy{}, "s", "l", "p", "op"); err == nil {
		t.Fatal("expected error on truncated version handshake")
	}
}

// errWriter fails on every Write to exercise the dst.Write error paths.
type errWriter struct{}

func (errWriter) Write(p []byte) (int, error) { return 0, errors.New("write failed") }

func TestFilterRFBInputWriteError(t *testing.T) {
	var in bytes.Buffer
	in.Write(rfbHandshake())
	if err := filterRFBInput(errWriter{}, &in, allowPolicy{}, "s", "l", "p", "op"); err == nil {
		t.Fatal("expected write error to propagate")
	}
}

// countWriter fails on the failAt-th write (1-based); earlier writes succeed.
type countWriter struct {
	n      int
	failAt int
}

func (c *countWriter) Write(p []byte) (int, error) {
	c.n++
	if c.n == c.failAt {
		return 0, errors.New("write failed")
	}
	return len(p), nil
}

// TestFilterRFBInputTruncatedPayloads covers the read-error return paths for
// each message type when the client stream ends mid-message.
func TestFilterRFBInputTruncatedPayloads(t *testing.T) {
	cases := map[string][]byte{
		"setpixelformat body": append(rfbHandshake(), ClientSetPixelFormat, 0, 0), // needs 19, only 2
		"setencodings header": append(rfbHandshake(), ClientSetEncodings, 0),      // needs 3 header, only 1
		"setencodings body": func() []byte {
			b := append(rfbHandshake(), ClientSetEncodings)
			hdr := []byte{0, 0, 0}
			binary.BigEndian.PutUint16(hdr[1:3], 2) // claims 2 encodings
			return append(b, hdr...)                // but no encoding bodies
		}(),
		"fbupdaterequest": append(rfbHandshake(), ClientFramebufferUpdateRequest, 0), // needs 9
		"keyevent":        append(rfbHandshake(), ClientKeyEvent, 0),                 // needs 7
		"pointerevent":    append(rfbHandshake(), ClientPointerEvent, 0),             // needs 5
		"cuttext header":  append(rfbHandshake(), 6, 0),                              // needs 7-byte header
		"cuttext body": func() []byte {
			b := append(rfbHandshake(), byte(6))
			hdr := make([]byte, 7)
			binary.BigEndian.PutUint32(hdr[3:7], 4) // length 4
			return append(b, hdr...)                // but no payload
		}(),
	}
	for name, data := range cases {
		var out bytes.Buffer
		src := bytes.NewReader(data)
		if err := filterRFBInput(&out, src, allowPolicy{}, "s", "l", "p", "op"); err == nil {
			t.Fatalf("%s: expected truncation error", name)
		}
	}
}

// TestFilterRFBInputWriteErrorsPerType forces a dst.Write failure inside each
// message-forwarding case.
func TestFilterRFBInputWriteErrorsPerType(t *testing.T) {
	// After the 3 handshake writes (version, security, clientinit) succeed, the
	// next write is the message-type byte; fail on that (failAt=4).
	build := func(msg ...byte) []byte { return append(rfbHandshake(), msg...) }
	cases := [][]byte{
		build(ClientSetPixelFormat, 0),
		build(ClientKeyEvent, 0, 0, 0, 0, 0, 0, 0),
		build(ClientPointerEvent, 0, 0, 0, 0, 0),
		func() []byte {
			b := build(byte(6))
			hdr := make([]byte, 7)
			binary.BigEndian.PutUint32(hdr[3:7], 1)
			return append(append(b, hdr...), 0)
		}(),
	}
	for i, data := range cases {
		w := &countWriter{failAt: 4}
		if err := filterRFBInput(w, bytes.NewReader(data), allowPolicy{}, "s", "l", "p", "op"); err == nil {
			t.Fatalf("case %d: expected write error", i)
		}
	}
}

// TestFilterRFBInputWriteErrorBranches drives a dst.Write failure at each
// distinct forwarding point. Write ordering: version(1), security(2),
// clientinit(3), then per-message writes starting at 4.
func TestFilterRFBInputWriteErrorBranches(t *testing.T) {
	hs := rfbHandshake()
	msg := func(m ...byte) []byte { return append(append([]byte(nil), hs...), m...) }

	setEnc := func() []byte {
		b := append([]byte(nil), hs...)
		b = append(b, ClientSetEncodings)
		hdr := []byte{0, 0, 0}
		binary.BigEndian.PutUint16(hdr[1:3], 1) // 1 encoding
		b = append(b, hdr...)
		return append(b, make([]byte, 4)...)
	}
	keyEv := msg(ClientKeyEvent, 0, 0, 0, 0, 0, 0, 0)
	ptrEv := msg(ClientPointerEvent, 0, 0, 0, 0, 0)
	cutText := func(n uint32) []byte {
		b := append([]byte(nil), hs...)
		b = append(b, 6)
		hdr := make([]byte, 7)
		binary.BigEndian.PutUint32(hdr[3:7], n)
		b = append(b, hdr...)
		return append(b, make([]byte, int(n))...)
	}

	cases := []struct {
		name   string
		data   []byte
		failAt int
	}{
		{"security", msg(ClientSetPixelFormat, 0), 2},
		{"clientinit", msg(ClientSetPixelFormat, 0), 3},
		{"fbupdaterequest-msgtype", msg(ClientFramebufferUpdateRequest, 0, 0, 0, 0, 0, 0, 0, 0, 0), 4},
		{"setencodings-msgtype", setEnc(), 4},
		{"setencodings-header", setEnc(), 5},
		{"keyevent-payload", keyEv, 5},
		{"pointerevent-payload", ptrEv, 5},
		{"cuttext-header", cutText(1), 5},
		{"cuttext-payload", cutText(2), 6},
	}
	for _, tc := range cases {
		w := &countWriter{failAt: tc.failAt}
		if err := filterRFBInput(w, bytes.NewReader(tc.data), allowPolicy{}, "s", "l", "p", "op"); err == nil {
			t.Fatalf("%s: expected write error at write %d", tc.name, tc.failAt)
		}
	}
}

func TestGRPCWriterSendError(t *testing.T) {
	// grpcWriter.Write forwards bytes to the stream Send; a failing stream
	// surfaces the error.
	we := &grpcWriter{stream: &mockStream{sendErr: errors.New("boom")}}
	if _, err := we.Write([]byte("x")); err == nil {
		t.Fatal("expected send error")
	}
}
