// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package vnc

import (
	"encoding/binary"
	"errors"
	"image"
	"net"
	"strings"
	"testing"
	"time"
)

// scriptedConn replays a byte script and records what was written to it, so a
// malformed conversation can be staged that no real server would produce on
// demand — a truncated stream, a security list without None, a ServerInit
// claiming a 60000-pixel desktop.
type scriptedConn struct {
	net.Conn
	server net.Conn
}

// newScriptedConn returns a client end fed by script, plus a handle on the
// server end so a test can push more bytes or close mid-conversation.
func newScriptedConn(t *testing.T, script []byte) *scriptedConn {
	t.Helper()
	client, server := net.Pipe()
	// A read that outlives the script must fail rather than hang: a truncated
	// handshake is one of the cases under test, and net.Pipe never reaches EOF
	// on its own.
	_ = client.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
	// net.Pipe is unbuffered, so the client's own handshake writes block until
	// something reads them. Drain continuously, or every test deadlocks on the
	// first ProtocolVersion the client sends back.
	go func() {
		buf := make([]byte, 4096)
		for {
			if _, err := server.Read(buf); err != nil {
				return
			}
		}
	}()
	go func() {
		if len(script) > 0 {
			_, _ = server.Write(script)
		}
	}()
	t.Cleanup(func() {
		_ = client.Close()
		_ = server.Close()
	})
	return &scriptedConn{Conn: client, server: server}
}

func serverInitBytes(width, height int, name string) []byte {
	header := make([]byte, 24)
	binary.BigEndian.PutUint16(header[0:2], uint16(width))
	binary.BigEndian.PutUint16(header[2:4], uint16(height))
	binary.BigEndian.PutUint32(header[20:24], uint32(len(name)))
	return append(header, []byte(name)...)
}

func handshakeBytes(width, height int) []byte {
	out := []byte("RFB 003.008\n")
	out = append(out, 1, SecurityNone) // one security type: None
	out = append(out, 0, 0, 0, 0)      // SecurityResult = ok
	return append(out, serverInitBytes(width, height, "")...)
}

func rawUpdateBytes(x, y, w, h int, pixels []byte) []byte {
	head := make([]byte, 4) // u8 type, u8 padding, u16 count
	binary.BigEndian.PutUint16(head[2:4], 1)
	rect := make([]byte, 12)
	binary.BigEndian.PutUint16(rect[0:2], uint16(x))
	binary.BigEndian.PutUint16(rect[2:4], uint16(y))
	binary.BigEndian.PutUint16(rect[4:6], uint16(w))
	binary.BigEndian.PutUint16(rect[6:8], uint16(h))
	binary.BigEndian.PutUint32(rect[8:12], uint32(EncodingRaw))
	return append(append(head, rect...), pixels...)
}

func TestEncodedMessagesAreTheSizesRfbSpecifies(t *testing.T) {
	if got := len(encodeSetPixelFormat()); got != 20 {
		t.Fatalf("SetPixelFormat = %d bytes, want 20", got)
	}
	if got := len(encodeSetEncodings()); got != 12 {
		t.Fatalf("SetEncodings = %d bytes, want 12", got)
	}
	full := encodeUpdateRequest(800, 600, false)
	if len(full) != 10 || full[1] != 0 {
		t.Fatalf("non-incremental request = %v", full)
	}
	if encodeUpdateRequest(800, 600, true)[1] != 1 {
		t.Fatal("incremental flag not set")
	}
}

func TestVersionPrefers38AndRefusesANonRfbPeer(t *testing.T) {
	conn := newScriptedConn(t, []byte("RFB 003.008\n"))
	version, err := negotiateVersion(conn)
	if err != nil || version != "RFB 003.008\n" {
		t.Fatalf("negotiate = %q, %v", version, err)
	}

	older := newScriptedConn(t, []byte("RFB 003.003\n"))
	if version, err = negotiateVersion(older); err != nil || version != "RFB 003.003\n" {
		t.Fatalf("older = %q, %v", version, err)
	}

	notRfb := newScriptedConn(t, []byte("HTTP/1.1 200"))
	if _, err = negotiateVersion(notRfb); err == nil || !strings.Contains(err.Error(), "not an RFB server") {
		t.Fatalf("non-RFB peer accepted: %v", err)
	}
}

func TestSecurityHandshakeAcrossVersions(t *testing.T) {
	ok38 := newScriptedConn(t, append([]byte{2, 1, 2}, 0, 0, 0, 0))
	if err := negotiateSecurity(ok38, "RFB 003.008\n"); err != nil {
		t.Fatalf("3.8 None: %v", err)
	}

	// 3.7 has no SecurityResult; reading one would desynchronise.
	ok37 := newScriptedConn(t, []byte{1, SecurityNone})
	if err := negotiateSecurity(ok37, "RFB 003.007\n"); err != nil {
		t.Fatalf("3.7 None: %v", err)
	}

	ok33 := newScriptedConn(t, []byte{0, 0, 0, 1})
	if err := negotiateSecurity(ok33, "RFB 003.003\n"); err != nil {
		t.Fatalf("3.3 None: %v", err)
	}

	bad33 := newScriptedConn(t, []byte{0, 0, 0, 2})
	if err := negotiateSecurity(bad33, "RFB 003.003\n"); err == nil {
		t.Fatal("3.3 accepted a type other than None")
	}
}

func TestSecurityRefusals(t *testing.T) {
	empty := newScriptedConn(t, []byte{0})
	if err := negotiateSecurity(empty, "RFB 003.008\n"); err == nil ||
		!strings.Contains(err.Error(), "offered no types") {
		t.Fatalf("empty list: %v", err)
	}

	// The offered list is reported in ascending order, as the other ports do.
	noNone := newScriptedConn(t, []byte{2, 16, 2})
	err := negotiateSecurity(noNone, "RFB 003.008\n")
	if err == nil || !strings.Contains(err.Error(), "offered 2, 16") {
		t.Fatalf("without None: %v", err)
	}

	rejected := newScriptedConn(t, append([]byte{1, SecurityNone}, 0, 0, 0, 1))
	if err = negotiateSecurity(rejected, "RFB 003.008\n"); err == nil ||
		!strings.Contains(err.Error(), "security rejected") {
		t.Fatalf("non-zero result: %v", err)
	}
}

func TestServerInitValidatesWhatTheServerClaims(t *testing.T) {
	conn := newScriptedConn(t, serverInitBytes(640, 480, "desktop"))
	width, height, err := readServerInit(conn)
	if err != nil || width != 640 || height != 480 {
		t.Fatalf("server init = %d,%d,%v", width, height, err)
	}

	for _, dims := range [][2]int{{0, 480}, {640, 0}, {60000, 480}, {640, 60000}} {
		hostile := newScriptedConn(t, serverInitBytes(dims[0], dims[1], ""))
		if _, _, err = readServerInit(hostile); err == nil ||
			!strings.Contains(err.Error(), "out of range") {
			t.Fatalf("%v accepted: %v", dims, err)
		}
	}

	longName := make([]byte, 24)
	binary.BigEndian.PutUint16(longName[0:2], 64)
	binary.BigEndian.PutUint16(longName[2:4], 64)
	binary.BigEndian.PutUint32(longName[20:24], 99999)
	if _, _, err = readServerInit(newScriptedConn(t, longName)); err == nil ||
		!strings.Contains(err.Error(), "name too long") {
		t.Fatalf("overlong name: %v", err)
	}
}

// dialTo returns a dialer handing back a prepared connection.
func dialTo(conn net.Conn) func(string, string, time.Duration) (net.Conn, error) {
	return func(string, string, time.Duration) (net.Conn, error) { return conn, nil }
}

func TestDialCompletesTheHandshakeAndTracksAnUpdate(t *testing.T) {
	script := append(handshakeBytes(4, 2), rawUpdateBytes(1, 0, 1, 1, []byte{255, 0, 0, 0})...)
	conn := newScriptedConn(t, script)

	client, err := DialRFB("host", 5900, time.Second, dialTo(conn))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = client.Close() }()

	if client.Width() != 4 || client.Height() != 2 {
		t.Fatalf("size = %dx%d", client.Width(), client.Height())
	}

	// The reader is a goroutine; give it the update before looking.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		shot, shotErr := client.Screenshot()
		if shotErr != nil {
			t.Fatalf("screenshot: %v", shotErr)
		}
		if rgba, ok := shot.(*image.RGBA); ok {
			if _, _, b, _ := rgba.At(1, 0).RGBA(); b > 0 {
				return // the blue pixel landed
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("raw rectangle never reached the framebuffer")
}

func TestDialReportsAFailureToConnect(t *testing.T) {
	failing := func(string, string, time.Duration) (net.Conn, error) {
		return nil, errors.New("connection refused")
	}
	if _, err := DialRFB("host", 5900, time.Second, failing); err == nil ||
		!strings.Contains(err.Error(), "rfb dial") {
		t.Fatalf("dial failure: %v", err)
	}
}

func TestDialClosesTheConnectionWhenTheHandshakeFails(t *testing.T) {
	conn := newScriptedConn(t, []byte("HTTP/1.1 200"))
	if _, err := DialRFB("host", 5900, time.Second, dialTo(conn)); err == nil {
		t.Fatal("a non-RFB peer produced a client")
	}
	// The connection is closed, so a write must fail.
	if _, err := conn.Write([]byte{0}); err == nil {
		t.Fatal("connection left open after a failed handshake")
	}
}

func TestInputIsWrittenAndRefusedAfterClose(t *testing.T) {
	conn := newScriptedConn(t, handshakeBytes(4, 2))
	client, err := DialRFB("host", 5900, time.Second, dialTo(conn))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}

	if err = client.InjectPointer(3, 1, 1); err != nil {
		t.Fatalf("pointer: %v", err)
	}
	if err = client.InjectKey(0xff0d, true); err != nil {
		t.Fatalf("key: %v", err)
	}

	if err = client.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	// Closing twice is a no-op, not an error.
	if err = client.Close(); err != nil {
		t.Fatalf("second close: %v", err)
	}
	if err = client.InjectPointer(0, 0, 0); err == nil ||
		!strings.Contains(err.Error(), "session is closed") {
		t.Fatalf("input after close: %v", err)
	}
}

func TestReadLoopStopsOnMalformedUpdates(t *testing.T) {
	cases := []struct {
		name   string
		suffix []byte
	}{
		{"a message it cannot skip blindly", []byte{2}},
		{"an absurd rectangle count", func() []byte {
			head := make([]byte, 4)
			binary.BigEndian.PutUint16(head[2:4], 9999)
			return head
		}()},
		{"a rectangle outside the framebuffer", func() []byte {
			head := make([]byte, 4)
			binary.BigEndian.PutUint16(head[2:4], 1)
			rect := make([]byte, 12)
			binary.BigEndian.PutUint16(rect[0:2], 3)
			binary.BigEndian.PutUint16(rect[4:6], 4)
			binary.BigEndian.PutUint16(rect[6:8], 1)
			binary.BigEndian.PutUint32(rect[8:12], uint32(EncodingRaw))
			return append(head, rect...)
		}()},
		{"an encoding it did not negotiate", func() []byte {
			head := make([]byte, 4)
			binary.BigEndian.PutUint16(head[2:4], 1)
			rect := make([]byte, 12)
			binary.BigEndian.PutUint16(rect[4:6], 1)
			binary.BigEndian.PutUint16(rect[6:8], 1)
			binary.BigEndian.PutUint32(rect[8:12], 7)
			return append(head, rect...)
		}()},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			conn := newScriptedConn(t, append(handshakeBytes(4, 2), tc.suffix...))
			client, err := DialRFB("host", 5900, time.Second, dialTo(conn))
			if err != nil {
				t.Fatalf("dial: %v", err)
			}
			// The loop must end on its own rather than spin or panic.
			select {
			case <-client.done:
			case <-time.After(2 * time.Second):
				t.Fatal("read loop did not stop")
			}
			_ = client.Close()
		})
	}
}

func TestZeroAreaAndCopyRectRectanglesAreConsumedExactly(t *testing.T) {
	head := make([]byte, 4)
	binary.BigEndian.PutUint16(head[2:4], 3)

	empty := make([]byte, 12)
	binary.BigEndian.PutUint32(empty[8:12], uint32(EncodingRaw))

	copyRect := make([]byte, 12)
	binary.BigEndian.PutUint16(copyRect[4:6], 1)
	binary.BigEndian.PutUint16(copyRect[6:8], 1)
	binary.BigEndian.PutUint32(copyRect[8:12], uint32(EncodingCopyRect))

	raw := make([]byte, 12)
	binary.BigEndian.PutUint16(raw[4:6], 1)
	binary.BigEndian.PutUint16(raw[6:8], 1)
	binary.BigEndian.PutUint32(raw[8:12], uint32(EncodingRaw))

	script := append(handshakeBytes(4, 2), head...)
	script = append(script, empty...)
	script = append(script, copyRect...)
	script = append(script, 0, 2, 0, 2) // copyrect source coordinates
	script = append(script, raw...)
	script = append(script, 9, 9, 9, 0)

	conn := newScriptedConn(t, script)
	client, err := DialRFB("host", 5900, time.Second, dialTo(conn))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = client.Close() }()

	// The raw rect only decodes if the two before it were consumed exactly.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		shot, _ := client.Screenshot()
		if rgba, ok := shot.(*image.RGBA); ok {
			if r, _, _, _ := rgba.At(0, 0).RGBA(); r > 0 {
				return
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("the rectangle after a copyrect never landed")
}

// failAfterWrites lets the Nth write onward fail, so each write-error branch in
// the handshake and the update loop can be reached without a flaky socket.
type failAfterWrites struct {
	net.Conn
	remaining int
}

func (f *failAfterWrites) Write(b []byte) (int, error) {
	if f.remaining <= 0 {
		return 0, errors.New("write refused")
	}
	f.remaining--
	return f.Conn.Write(b)
}

func TestHandshakeWriteFailuresAreReported(t *testing.T) {
	// Each index is one more successful write before the failure, walking the
	// failure through ProtocolVersion, security select, ClientInit,
	// SetPixelFormat, SetEncodings and the first update request in turn.
	for allowed := 0; allowed <= 5; allowed++ {
		conn := newScriptedConn(t, handshakeBytes(4, 2))
		failing := &failAfterWrites{Conn: conn, remaining: allowed}
		if _, err := DialRFB("host", 5900, time.Second, dialTo(failing)); err == nil {
			t.Fatalf("write failure after %d writes produced a client", allowed)
		}
	}
}

func TestTruncatedHandshakesAreReported(t *testing.T) {
	full := handshakeBytes(4, 2)
	// Cut the script at each stage boundary: a peer that goes away mid
	// handshake must be an error, never a half-built session.
	for _, cut := range []int{0, 12, 13, 14, 18, 30} {
		conn := newScriptedConn(t, full[:cut])
		if _, err := DialRFB("host", 5900, time.Second, dialTo(conn)); err == nil {
			t.Fatalf("truncation at %d produced a client", cut)
		}
	}
}

func TestScreenshotAndCloseAfterThePeerGoesAway(t *testing.T) {
	conn := newScriptedConn(t, handshakeBytes(4, 2))
	client, err := DialRFB("host", 5900, time.Second, dialTo(conn))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	// The peer closing ends the loop; a screenshot still answers, and Close
	// still returns rather than blocking on a finished goroutine.
	_ = conn.server.Close()
	select {
	case <-client.done:
	case <-time.After(2 * time.Second):
		t.Fatal("read loop did not notice the peer leaving")
	}
	if _, err = client.Screenshot(); err != nil {
		t.Fatalf("screenshot after peer left: %v", err)
	}
	_ = client.Close()
}

func TestInjectingAfterThePeerLeavesReportsTheWriteError(t *testing.T) {
	conn := newScriptedConn(t, handshakeBytes(4, 2))
	client, err := DialRFB("host", 5900, time.Second, dialTo(conn))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = client.Close() }()
	_ = conn.server.Close()
	<-client.done
	// Still "open" as far as the session knows, so the write is attempted and
	// its failure surfaces rather than being swallowed.
	if err = client.InjectKey(0xff0d, false); err == nil {
		t.Fatal("a write to a dead peer reported success")
	}
}

func TestSwapBGRAToRGBALeavesEveryPixelOpaque(t *testing.T) {
	pixels := []byte{1, 2, 3, 0, 4, 5, 6, 0}
	swapBGRAToRGBA(pixels)
	// Blue and red exchanged, green untouched, alpha forced opaque.
	if got := string(pixels); got != string([]byte{3, 2, 1, 255, 6, 5, 4, 255}) {
		t.Fatalf("swapped = %v", []byte(got))
	}
}
