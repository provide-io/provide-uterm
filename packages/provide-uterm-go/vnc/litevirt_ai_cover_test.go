package vnc

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"testing"
	"time"

	pb "github.com/litevirt/litevirt/gen/litevirt/v1"
)

// rfbServerHandshake builds a valid server->client RFB byte stream up to and
// including ServerInit for a widthxheight desktop with an empty name.
func rfbServerHandshake(width, height uint16) []byte {
	var b bytes.Buffer
	b.WriteString("RFB 003.008\n") // 12-byte version
	b.WriteByte(1)                 // numSecTypes
	b.WriteByte(1)                 // security type None(1)
	b.Write([]byte{0, 0, 0, 0})    // SecurityResult OK
	// ServerInit: width, height, 16-byte pixel format, 4-byte name length(0).
	si := make([]byte, 24)
	binary.BigEndian.PutUint16(si[0:2], width)
	binary.BigEndian.PutUint16(si[2:4], height)
	// nameLen at [20:24] stays 0.
	b.Write(si)
	return b.Bytes()
}

func newAIClientFromStream(data []byte) *LitevirtAIClient {
	return &LitevirtAIClient{
		stream:  &mockStream{recvData: [][]byte{data}},
		tracker: NewFramebufferTracker(10, 10),
		ready:   make(chan struct{}),
	}
}

func TestRunHandshakeAndLoopFullSequence(t *testing.T) {
	var b bytes.Buffer
	b.Write(rfbServerHandshake(2, 2))

	// FramebufferUpdate (type 0): pad + numRects=1.
	b.WriteByte(0)
	b.Write([]byte{0, 0, 1})
	// One rect covering 0,0 2x2, encoding Raw(0).
	rect := make([]byte, 12)
	binary.BigEndian.PutUint16(rect[4:6], 2) // w
	binary.BigEndian.PutUint16(rect[6:8], 2) // h
	b.Write(rect)
	b.Write(make([]byte, 2*2*4)) // pixels

	// ColourMapEntries (type 1): pad + first(2) + number(2)=0.
	b.WriteByte(1)
	b.Write([]byte{0, 0, 0, 0, 0})

	// Bell (type 2): no body.
	b.WriteByte(2)

	// ServerCutText (type 3): pad(3) + length(4)=3 + payload.
	b.WriteByte(3)
	cut := make([]byte, 7)
	binary.BigEndian.PutUint32(cut[3:7], 3)
	b.Write(cut)
	b.Write([]byte("abc"))

	// Stream ends -> next msgType read hits EOF and loop returns.
	c := newAIClientFromStream(b.Bytes())
	err := c.RunHandshakeAndLoop()
	if err == nil {
		t.Fatal("expected EOF-terminated loop to return an error")
	}
	// Handshake completed, so ready must be closed with nil error.
	if werr := c.WaitReady(context.Background()); werr != nil {
		t.Fatalf("WaitReady after successful handshake: %v", werr)
	}
}

func TestRunHandshakeBadVersion(t *testing.T) {
	c := newAIClientFromStream([]byte("XXX 003.008\n"))
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected unsupported version error")
	}
}

func TestRunHandshakeSecurityFailedNumTypesZero(t *testing.T) {
	var b bytes.Buffer
	b.WriteString("RFB 003.008\n")
	b.WriteByte(0) // numSecTypes == 0 -> read reason
	reason := "nope"
	rl := make([]byte, 4)
	binary.BigEndian.PutUint32(rl, uint32(len(reason)))
	b.Write(rl)
	b.WriteString(reason)
	c := newAIClientFromStream(b.Bytes())
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected connection-failed error")
	}
}

func TestRunHandshakeNoNoneSecurity(t *testing.T) {
	var b bytes.Buffer
	b.WriteString("RFB 003.008\n")
	b.WriteByte(1) // one sec type
	b.WriteByte(2) // type 2, not None(1)
	c := newAIClientFromStream(b.Bytes())
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected 'None not supported' error")
	}
}

func TestRunHandshakeSecurityResultNonZero(t *testing.T) {
	var b bytes.Buffer
	b.WriteString("RFB 003.008\n")
	b.WriteByte(1)
	b.WriteByte(1)
	b.Write([]byte{0, 0, 0, 1}) // SecurityResult != 0
	c := newAIClientFromStream(b.Bytes())
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected security-failed error")
	}
}

func TestRunHandshakeInvalidDimensions(t *testing.T) {
	// width 0 -> invalid framebuffer dimensions.
	c := newAIClientFromStream(rfbServerHandshake(0, 10))
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected invalid-dimensions error")
	}
}

func TestRunHandshakeUnsupportedServerMessage(t *testing.T) {
	var b bytes.Buffer
	b.Write(rfbServerHandshake(2, 2))
	b.WriteByte(99) // unknown server message type
	c := newAIClientFromStream(b.Bytes())
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected unsupported server message error")
	}
}

func TestRunHandshakeUnsupportedEncoding(t *testing.T) {
	var b bytes.Buffer
	b.Write(rfbServerHandshake(2, 2))
	b.WriteByte(0)           // FramebufferUpdate
	b.Write([]byte{0, 0, 1}) // 1 rect
	rect := make([]byte, 12)
	binary.BigEndian.PutUint16(rect[4:6], 2)
	binary.BigEndian.PutUint16(rect[6:8], 2)
	binary.BigEndian.PutUint32(rect[8:12], 5) // non-raw encoding
	b.Write(rect)
	c := newAIClientFromStream(b.Bytes())
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected unsupported-encoding error")
	}
}

func TestRunHandshakeTruncatedPixels(t *testing.T) {
	var b bytes.Buffer
	b.Write(rfbServerHandshake(2, 2))
	b.WriteByte(0)           // FramebufferUpdate
	b.Write([]byte{0, 0, 1}) // 1 rect
	rect := make([]byte, 12)
	binary.BigEndian.PutUint16(rect[4:6], 2)
	binary.BigEndian.PutUint16(rect[6:8], 2)
	b.Write(rect)
	b.Write(make([]byte, 4)) // only 4 of the 16 expected pixel bytes
	c := newAIClientFromStream(b.Bytes())
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected truncated-pixels error")
	}
}

func TestRunHandshakeInvalidRectBounds(t *testing.T) {
	var b bytes.Buffer
	b.Write(rfbServerHandshake(2, 2))
	b.WriteByte(0)
	b.Write([]byte{0, 0, 1})
	rect := make([]byte, 12)
	binary.BigEndian.PutUint16(rect[4:6], 5) // w=5 > width 2
	binary.BigEndian.PutUint16(rect[6:8], 2)
	b.Write(rect)
	c := newAIClientFromStream(b.Bytes())
	if err := c.RunHandshakeAndLoop(); err == nil {
		t.Fatal("expected invalid-rect-bounds error")
	}
}

func TestWaitReadyContextCancelled(t *testing.T) {
	c := &LitevirtAIClient{ready: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := c.WaitReady(ctx); err == nil {
		t.Fatal("expected context cancellation error")
	}
}

func TestWaitReadyReturnsStoredErr(t *testing.T) {
	c := &LitevirtAIClient{ready: make(chan struct{})}
	c.markReady(errors.New("handshake boom"))
	// markReady is idempotent; a second call must not double-close.
	c.markReady(errors.New("ignored"))
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := c.WaitReady(ctx); err == nil || err.Error() != "handshake boom" {
		t.Fatalf("expected stored handshake error, got %v", err)
	}
}

func TestLitevirtAIClientClose(t *testing.T) {
	c := &LitevirtAIClient{}
	if err := c.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
}

func TestScreenshotNilTracker(t *testing.T) {
	c := &LitevirtAIClient{}
	if _, err := c.Screenshot(); err == nil {
		t.Fatal("expected error when tracker is nil")
	}
}

// sendFailStream fails Send after failAfter successful sends; Recv drains data.
type sendFailStream struct {
	mockStream
	failAfter int
	sends     int
}

func (s *sendFailStream) Send(*pb.VNCData) error {
	s.sends++
	if s.sends > s.failAfter {
		return errors.New("send failed")
	}
	return nil
}

func runAI(data []byte) error {
	c := &LitevirtAIClient{
		stream:  &mockStream{recvData: [][]byte{data}},
		tracker: NewFramebufferTracker(10, 10),
		ready:   make(chan struct{}),
	}
	return c.RunHandshakeAndLoop()
}

func runAISendFail(data []byte, failAfter int) error {
	c := &LitevirtAIClient{
		stream:  &sendFailStream{mockStream: mockStream{recvData: [][]byte{data}}, failAfter: failAfter},
		tracker: NewFramebufferTracker(10, 10),
		ready:   make(chan struct{}),
	}
	return c.RunHandshakeAndLoop()
}

// TestRunHandshakeReadTruncations covers the io.ReadFull error returns at each
// handshake / message stage when the server stream ends prematurely.
func TestRunHandshakeReadTruncations(t *testing.T) {
	full := rfbServerHandshake(2, 2)
	appendMsg := func(msg ...byte) []byte { return append(append([]byte(nil), full...), msg...) }

	cases := map[string][]byte{
		"numSecTypes":  []byte("RFB 003.008\n"),
		"reasonLen":    []byte("RFB 003.008\n\x00"),                     // numSecTypes=0, no reason len
		"reasonStr":    []byte("RFB 003.008\n\x00\x00\x00\x00\x05"),     // reasonLen=5, no reason
		"secTypes":     []byte("RFB 003.008\n\x01"),                     // numSecTypes=1, no types
		"secResult":    []byte("RFB 003.008\n\x01\x01"),                 // types ok, no SecurityResult
		"serverInit":   []byte("RFB 003.008\n\x01\x01\x00\x00\x00\x00"), // secResult ok, no ServerInit
		"fbUpdHeader":  appendMsg(0),                                    // msgType 0, no header
		"fbRect":       appendMsg(0, 0, 0, 1),                           // 1 rect, no rect body
		"colourHeader": appendMsg(1),                                    // msgType 1, no header
		"cutHeader":    appendMsg(3),                                    // msgType 3, no header
	}
	for name, data := range cases {
		if err := runAI(data); err == nil {
			t.Fatalf("%s: expected truncation error", name)
		}
	}
}

func TestRunHandshakeReasonTooLong(t *testing.T) {
	var b []byte
	b = append(b, []byte("RFB 003.008\n")...)
	b = append(b, 0)                      // numSecTypes=0
	b = append(b, 0x00, 0x00, 0x20, 0x00) // reasonLen = 8192 > maxRFBNameLen
	if err := runAI(b); err == nil {
		t.Fatal("expected reason-too-long error")
	}
}

func TestRunHandshakeNameTooLong(t *testing.T) {
	// ServerInit with an oversized desktop-name length.
	var si []byte
	si = append(si, []byte("RFB 003.008\n\x01\x01\x00\x00\x00\x00")...) // through secResult
	init := make([]byte, 24)
	binary.BigEndian.PutUint16(init[0:2], 2)      // width
	binary.BigEndian.PutUint16(init[2:4], 2)      // height
	binary.BigEndian.PutUint32(init[20:24], 5000) // nameLen > maxRFBNameLen
	si = append(si, init...)
	if err := runAI(si); err == nil {
		t.Fatal("expected desktop-name-too-long error")
	}
}

func TestRunHandshakeNameTruncated(t *testing.T) {
	var si []byte
	si = append(si, []byte("RFB 003.008\n\x01\x01\x00\x00\x00\x00")...)
	init := make([]byte, 24)
	binary.BigEndian.PutUint16(init[0:2], 2)
	binary.BigEndian.PutUint16(init[2:4], 2)
	binary.BigEndian.PutUint32(init[20:24], 4) // nameLen 4, but no name bytes follow
	si = append(si, init...)
	if err := runAI(si); err == nil {
		t.Fatal("expected name-copy truncation error")
	}
}

func TestRunHandshakeColourMapAndCutText(t *testing.T) {
	full := rfbServerHandshake(2, 2)

	// ColourMapEntries with num>0 but truncated palette -> CopyN error.
	cm := append(append([]byte(nil), full...), 1) // msgType 1
	cmHdr := []byte{0, 0, 0, 0, 2}                // number = 2 -> expects 12 bytes
	cm = append(cm, cmHdr...)
	if err := runAI(cm); err == nil {
		t.Fatal("expected colourmap CopyN truncation error")
	}

	// ServerCutText too large.
	ct := append(append([]byte(nil), full...), 3)
	big := make([]byte, 7)
	binary.BigEndian.PutUint32(big[3:7], (1<<20)+1) // > maxRFBCutText
	ct = append(ct, big...)
	if err := runAI(ct); err == nil {
		t.Fatal("expected cut-text-too-large error")
	}

	// ServerCutText with length>0 but truncated body -> CopyN error.
	ct2 := append(append([]byte(nil), full...), 3)
	body := make([]byte, 7)
	binary.BigEndian.PutUint32(body[3:7], 5)
	ct2 = append(ct2, body...)
	if err := runAI(ct2); err == nil {
		t.Fatal("expected cut-text CopyN truncation error")
	}
}

// TestRunHandshakeSendErrors covers each stream.Send failure branch by failing
// the Nth send (counting version=1, security=2, clientinit=3, setpf/enc=4, req=5).
func TestRunHandshakeSendErrors(t *testing.T) {
	full := rfbServerHandshake(2, 2)
	// Build a stream that reaches the FramebufferUpdateRequest send successfully.
	withUpdate := func() []byte {
		b := append([]byte(nil), full...)
		b = append(b, 0, 0, 0, 1) // fbupdate, header numRects=1
		rect := make([]byte, 12)
		binary.BigEndian.PutUint16(rect[4:6], 2)
		binary.BigEndian.PutUint16(rect[6:8], 2)
		b = append(b, rect...)
		b = append(b, make([]byte, 16)...) // pixels
		return b
	}

	cases := []struct {
		name      string
		data      []byte
		failAfter int
	}{
		{"version", []byte("RFB 003.008\n"), 0},
		{"security", []byte("RFB 003.008\n\x01\x01"), 1},
		{"clientinit", []byte("RFB 003.008\n\x01\x01\x00\x00\x00\x00"), 2},
		{"setpixelformat", full, 3},
		{"fbrequest", withUpdate(), 4},
	}
	for _, tc := range cases {
		if err := runAISendFail(tc.data, tc.failAfter); err == nil {
			t.Fatalf("%s: expected send error", tc.name)
		}
	}
}
