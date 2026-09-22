//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

// Every transport reports the end of a connection as a typed close: which side
// ended it, plus the protocol's code and reason when it has them. Port of the
// Python contract in provide/uterm/transport_close.py (dc87968c) and its tests
// test_transport_close.py / test_transport_close_mapping.py. The cases the
// ports share (summary, WebSocket attribution, telnet, chaos) are in
// close_vectors_test.go; these are the Go-specific ones.

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"strings"
	"syscall"
	"testing"
	"time"
)

func codePtr(n int) *int { return &n }

func TestCloseInitiatorSpelling(t *testing.T) {
	if CloseLocal != "local" || CloseRemote != "remote" || CloseUnknown != "unknown" {
		t.Errorf("initiators = %q %q %q", CloseLocal, CloseRemote, CloseUnknown)
	}
}

func TestTransportClosedErrorCarriesTheClose(t *testing.T) {
	tc := TransportClose{Initiator: CloseLocal, Code: codePtr(1011), Reason: "keepalive ping timeout"}
	cause := io.ErrUnexpectedEOF
	err := closedError("connection closed", tc, cause)

	if got := err.Error(); got != "connection closed (local close 1011 keepalive ping timeout)" {
		t.Errorf("Error() = %q", got)
	}
	if !errors.Is(err, ErrConnectionClosed) {
		t.Error("a closed error must match ErrConnectionClosed")
	}
	if errors.Is(err, ErrNotConnected) {
		t.Error("a closed error is not ErrNotConnected")
	}
	if !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Error("the cause must stay reachable through Unwrap")
	}
	var closedErr *TransportClosedError
	if !errors.As(err, &closedErr) {
		t.Fatal("errors.As must expose *TransportClosedError")
	}
	if closedErr.Close.Summary() != tc.Summary() || closedErr.Message != "connection closed" {
		t.Errorf("closedErr = %+v", closedErr)
	}
	// Wrapped further, it is still found.
	if !errors.As(errors.Join(errors.New("outer"), err), &closedErr) {
		t.Error("errors.As must see through wrapping")
	}
	if (&TransportClosedError{}).Unwrap() != nil {
		t.Error("no cause unwraps to nil")
	}
}

func TestCloseFromError(t *testing.T) {
	err := &net.OpError{Op: "read", Net: "tcp", Err: os.NewSyscallError("read", syscall.ECONNRESET)}
	tc := CloseFromError(err, CloseRemote)
	if tc.Initiator != CloseRemote || tc.Code != nil || tc.Reason != "" {
		t.Errorf("close = %+v", tc)
	}
	if want := "*net.OpError: " + err.Error(); tc.Detail != want {
		t.Errorf("detail = %q, want %q", tc.Detail, want)
	}
}

// --- WebSocket ---------------------------------------------------------------

func TestCloseFromWSFramesCopiesTheCode(t *testing.T) {
	rcvd := &wsCloseFrame{1001, "going away"}
	got := closeFromWSFrames(rcvd, nil, false, "")
	*got.Code = 1
	if rcvd.code != 1001 {
		t.Error("the close must not alias the frame")
	}
	sent := &wsCloseFrame{1000, ""}
	got = closeFromWSFrames(nil, sent, false, "")
	*got.Code = 1
	if sent.code != 1000 {
		t.Error("the close must not alias the sent frame")
	}
}

func TestCloseFromWSReadErrorPlainError(t *testing.T) {
	got := closeFromWSReadError(io.ErrUnexpectedEOF, nil)
	if got.Initiator != CloseUnknown || got.Code != nil || got.Detail != io.ErrUnexpectedEOF.Error() {
		t.Errorf("close = %+v", got)
	}
	// Our own close frame and no received one: local, with our code.
	got = closeFromWSReadError(context.Canceled, &wsCloseFrame{1000, ""})
	if got.Initiator != CloseLocal || got.Code == nil || *got.Code != 1000 {
		t.Errorf("close = %+v", got)
	}
}

// --- telnet ------------------------------------------------------------------

func TestCloseFromSocketError(t *testing.T) {
	reset := &net.OpError{Op: "read", Net: "tcp", Err: os.NewSyscallError("read", syscall.ECONNRESET)}
	pipe := &net.OpError{Op: "write", Net: "tcp", Err: os.NewSyscallError("write", syscall.EPIPE)}
	if got := closeFromSocketError(reset); got.Initiator != CloseRemote || !strings.Contains(got.Detail, "connection reset") {
		t.Errorf("reset close = %+v", got)
	}
	if got := closeFromSocketError(pipe); got.Initiator != CloseUnknown || !strings.Contains(got.Detail, "broken pipe") {
		t.Errorf("pipe close = %+v", got)
	}
}

// fakeConn is a net.Conn whose Read/Write fail with fixed errors.
type fakeConn struct {
	net.Conn
	readErr, writeErr error
}

func (c *fakeConn) Read([]byte) (int, error)         { return 0, c.readErr }
func (c *fakeConn) Write([]byte) (int, error)        { return 0, c.writeErr }
func (c *fakeConn) Close() error                     { return nil }
func (c *fakeConn) SetReadDeadline(time.Time) error  { return nil }
func (c *fakeConn) RemoteAddr() net.Addr             { return &net.TCPAddr{} }
func (c *fakeConn) SetWriteDeadline(time.Time) error { return nil }

func telnetOn(conn net.Conn) *TelnetTransport {
	tr := NewTelnetTransport()
	tr.conn = conn
	return tr
}

func requireClose(t *testing.T, err error, prefix string, initiator CloseInitiator) TransportClose {
	t.Helper()
	var closedErr *TransportClosedError
	if !errors.As(err, &closedErr) {
		t.Fatalf("want *TransportClosedError, got %T %v", err, err)
	}
	if !strings.HasPrefix(err.Error(), prefix+" (") {
		t.Errorf("message %q does not start with %q", err.Error(), prefix)
	}
	if closedErr.Close.Initiator != initiator {
		t.Errorf("initiator = %q, want %q (%v)", closedErr.Close.Initiator, initiator, err)
	}
	return closedErr.Close
}

func TestTelnetReceiveCloses(t *testing.T) {
	reset := &net.OpError{Op: "read", Net: "tcp", Err: os.NewSyscallError("read", syscall.ECONNRESET)}
	pipe := &net.OpError{Op: "read", Net: "tcp", Err: os.NewSyscallError("read", syscall.EPIPE)}
	cases := []struct {
		name      string
		readErr   error
		prefix    string
		initiator CloseInitiator
		summary   string
	}{
		{"eof", io.EOF, "connection closed by remote", CloseRemote, "remote close"},
		// A zero-byte read with no error is EOF too.
		{"zero-read", nil, "connection closed by remote", CloseRemote, "remote close"},
		{"reset", reset, "connection lost", CloseRemote, "remote close (*net.OpError: " + reset.Error() + ")"},
		{"broken-pipe", pipe, "connection lost", CloseUnknown, "unknown close (*net.OpError: " + pipe.Error() + ")"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tr := telnetOn(&fakeConn{readErr: tc.readErr})
			_, err := tr.Receive(context.Background(), 64, time.Second)
			got := requireClose(t, err, tc.prefix, tc.initiator)
			if got.Summary() != tc.summary {
				t.Errorf("summary = %q, want %q", got.Summary(), tc.summary)
			}
			if tc.readErr != nil && !errors.Is(err, tc.readErr) {
				t.Error("the read error must stay reachable")
			}
			if tr.IsConnected() {
				t.Error("a closed telnet transport must disconnect")
			}
		})
	}
}

func TestTelnetSendCloses(t *testing.T) {
	reset := &net.OpError{Op: "write", Net: "tcp", Err: os.NewSyscallError("write", syscall.ECONNRESET)}
	pipe := &net.OpError{Op: "write", Net: "tcp", Err: os.NewSyscallError("write", syscall.EPIPE)}
	for _, tc := range []struct {
		err       error
		initiator CloseInitiator
	}{{reset, CloseRemote}, {pipe, CloseUnknown}} {
		tr := telnetOn(&fakeConn{writeErr: tc.err})
		err := tr.Send(context.Background(), []byte("x"))
		requireClose(t, err, "send failed", tc.initiator)
		var netErr net.Error
		if !errors.As(err, &netErr) {
			t.Error("the socket error must stay reachable for retry classification")
		}
	}
}

// --- chaos -------------------------------------------------------------------

func TestChaosInjectedDisconnectIsAnUnknownClose(t *testing.T) {
	c := NewChaosTransport(newStubTransport(), ChaosConfig{DisconnectEveryNReceives: 1, Label: "lbl"})
	_, err := c.Receive(context.Background(), 16, time.Millisecond)
	// Byte-identical to the Python/TypeScript message.
	if want := "lbl: injected disconnect on receive #1 (unknown close injected disconnect)"; err == nil || err.Error() != want {
		t.Errorf("err = %v, want %q", err, want)
	}
	got := requireClose(t, err, "lbl: injected disconnect on receive #1", CloseUnknown)
	if got.Reason != "injected disconnect" {
		t.Errorf("reason = %q", got.Reason)
	}
}
