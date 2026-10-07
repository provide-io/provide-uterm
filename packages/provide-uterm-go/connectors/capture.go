//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"fmt"
	"sync"
	"time"

	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// capturePollInterval is how long CaptureTransport.Receive sleeps between
// drains of the capture queue while it waits for output.
const capturePollInterval = 10 * time.Millisecond

// newCapture builds a "pty_capture" connector: it observes a shell that
// libuterm_capture has hooked (the PAM capture-mode login) through the Unix
// socket that library writes to. No process is forked. Port of the Python
// registration register_connector("pty_capture", CaptureConnector).
//
// The pty package's CaptureConnector speaks the reference's frame-returning
// shape (poll_messages, get_snapshot), not this package's Connector interface,
// so it is driven as a transport: CaptureTransport turns its polled stdout into
// a byte stream, and the shared transportConnector puts the same
// TransportSession and emulator over it that every other connector has. That
// is what the runtime needs — a Session() for the worker bridge to watch, and a
// snapshot that comes from an emulated screen.
//
// pty imports nothing else from this module, so importing it here closes no
// cycle and needs no registration hook.
func newCapture(sessionID, displayName string, config map[string]any) (*transportConnector, error) {
	// Validation (allowed keys, required socket_path) is the pty connector's
	// own, so the two cannot drift apart.
	capture, err := uptty.NewCaptureConnector(sessionID, displayName, config)
	if err != nil {
		return nil, err
	}
	inputMode := configStr(config, "input_mode", "open")
	cols := configInt(config, "cols", 80)
	rows := configInt(config, "rows", 24)

	build := func() *termsession.TransportSession {
		tr := NewCaptureTransport(capture)
		connect := func(ctx context.Context) error {
			return tr.Connect(ctx, "", 0, transports.ConnectOptions{Cols: cols, Rows: rows})
		}
		return termsession.New(tr, connect, termsession.Options{
			Cols:         cols,
			Rows:         rows,
			SendEncoding: termsession.EncodingUTF8,
		})
	}
	upstream := fmt.Sprintf("unix:%v", config["socket_path"])
	return newTransportConnector(sessionID, displayName, "pty_capture", upstream, inputMode, build), nil
}

// CaptureTransport adapts a pty.CaptureConnector to transports.ConnectionTransport.
// Connect binds the capture socket, Receive yields the captured stdout (already
// CRLF-normalised by the connector), and Send forwards keystrokes to the stdin
// socket when one is configured.
type CaptureTransport struct {
	capture *uptty.CaptureConnector

	mu      sync.Mutex
	pending []byte
}

var _ transports.ConnectionTransport = (*CaptureTransport)(nil)

// NewCaptureTransport wraps capture. It does not bind anything until Connect.
func NewCaptureTransport(capture *uptty.CaptureConnector) *CaptureTransport {
	return &CaptureTransport{capture: capture}
}

// Connect binds the capture socket. host, port and opts are meaningless for a
// local socket the connector already knows the path of.
func (t *CaptureTransport) Connect(ctx context.Context, _ string, _ int, _ transports.ConnectOptions) error {
	return t.capture.Start(ctx)
}

// Disconnect unbinds the capture socket and closes any stdin forwarder.
func (t *CaptureTransport) Disconnect(ctx context.Context) error {
	return t.capture.Stop(ctx)
}

// Send forwards input to the captured shell's stdin socket. Without one the
// session is observe-only and the input is dropped, as in the reference.
func (t *CaptureTransport) Send(ctx context.Context, data []byte) error {
	t.capture.HandleInput(ctx, string(data))
	return nil
}

// IsConnected reports whether the capture socket is bound.
func (t *CaptureTransport) IsConnected() bool { return t.capture.IsConnected() }

// Receive returns up to maxBytes of captured stdout, polling the capture queue
// until output arrives or timeout passes (then an empty slice, nil error). Once
// the socket is unbound it reports transports.ErrConnectionClosed, which is what
// ends the session's reader goroutine.
func (t *CaptureTransport) Receive(ctx context.Context, maxBytes int, timeout time.Duration) ([]byte, error) {
	deadline := time.Now().Add(timeout)
	for {
		t.mu.Lock()
		for _, frame := range t.capture.PollMessages() {
			data, _ := frame["data"].(string)
			t.pending = append(t.pending, data...)
		}
		if len(t.pending) > 0 {
			n := min(len(t.pending), maxBytes)
			out := append([]byte(nil), t.pending[:n]...)
			t.pending = t.pending[n:]
			t.mu.Unlock()
			return out, nil
		}
		t.mu.Unlock()
		if !t.capture.IsConnected() {
			return nil, transports.ErrConnectionClosed
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return []byte{}, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(min(remaining, capturePollInterval)):
		}
	}
}
