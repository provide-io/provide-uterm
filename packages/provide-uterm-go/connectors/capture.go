//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"bytes"
	"context"
	"fmt"
	"regexp"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/emulator"
	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// capturePollInterval is how long CaptureTransport.Receive sleeps between
// drains of the capture queue while it waits for output.
const capturePollInterval = 10 * time.Millisecond

// captureFullClear matches a full clear: erase display, switch to the
// alternate screen, or full reset. A full-screen program begins every whole
// paint with one, so the output after the last of them is what draws the
// current screen from nothing. Port of capture_connector.py _FULL_CLEAR.
var captureFullClear = regexp.MustCompile(`\x1b\[2J|\x1b\[\?1049h|\x1bc`)

// captureReplayMax bounds that output; past it a redraw starts mid-paint and
// settles at the next clear. Port of capture_connector.py _REPLAY_MAX.
const captureReplayMax = 512 * 1024

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
// pty imports only screen from this module, which imports nothing from it, so
// importing pty here closes no cycle and needs no registration hook.
func newCapture(sessionID, displayName string, config map[string]any) (*transportConnector, error) {
	// Validation (allowed keys, required socket_path) is the pty connector's
	// own, so the two cannot drift apart.
	capture, err := uptty.NewCaptureConnector(sessionID, displayName, config)
	if err != nil {
		return nil, err
	}
	inputMode := configStr(config, "input_mode", "open")

	build := func() *termsession.TransportSession {
		// The size the capture side holds, not the configured one: after a
		// geometry frame they differ, and a restarted session built at the
		// old size would never be told the new one again.
		cols, rows := capture.Size()
		tr := NewCaptureTransport(capture)
		connect := func(ctx context.Context) error {
			return tr.Connect(ctx, "", 0, transports.ConnectOptions{Cols: cols, Rows: rows})
		}
		// UTF-8, as the reference's capture emulator decodes: the shim
		// captures what a local program writes.
		sess := termsession.New(tr, connect, termsession.Options{
			Cols:            cols,
			Rows:            rows,
			SendEncoding:    termsession.EncodingUTF8,
			ReceiveEncoding: emulator.UTF8,
		})
		// A geometry frame on the capture wire resizes the session viewers
		// see, redrawn from what was drawn since the last clear.
		tr.OnResize(sess.Resize)
		return sess
	}
	upstream := fmt.Sprintf("unix:%v", config["socket_path"])
	return newTransportConnector(sessionID, displayName, "pty_capture", upstream, inputMode, build), nil
}

// CaptureTransport adapts a pty.CaptureConnector to transports.ConnectionTransport.
// Connect binds the capture socket, Receive yields the captured stdout (already
// CRLF-normalised by the connector), and Send forwards keystrokes to the stdin
// socket when one is configured. A geometry frame, which the connector answers
// with a snapshot at the new size, reaches the OnResize hook.
type CaptureTransport struct {
	capture *uptty.CaptureConnector

	mu       sync.Mutex
	pending  []byte
	onResize func(cols, rows int, redraw []byte)
	// sinceClear is the output since the last full clear, the redraw handed
	// to onResize. resize is a size not yet applied: it waits until every
	// byte that preceded it has been handed to the reader, so that output
	// is not drawn after the redraw that already contains it.
	sinceClear []byte
	resize     *[2]int
}

var _ transports.ConnectionTransport = (*CaptureTransport)(nil)

// NewCaptureTransport wraps capture. It does not bind anything until Connect.
func NewCaptureTransport(capture *uptty.CaptureConnector) *CaptureTransport {
	return &CaptureTransport{capture: capture}
}

// OnResize sets the hook a geometry frame reaches: the new size and the output
// since the last full clear, to redraw the screen from. It is called from
// Receive, between reads, once all output that preceded the frame has been
// returned.
func (t *CaptureTransport) OnResize(hook func(cols, rows int, redraw []byte)) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.onResize = hook
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
			if frame["type"] == "snapshot" {
				// The connector repaints after a geometry frame; the size is
				// what this side needs from it.
				cols, _ := frame["cols"].(int)
				rows, _ := frame["rows"].(int)
				t.resize = &[2]int{cols, rows}
				continue
			}
			data, _ := frame["data"].(string)
			t.pending = append(t.pending, data...)
			t.noteOutput([]byte(data))
		}
		if len(t.pending) == 0 && t.resize != nil {
			size, hook, redraw := *t.resize, t.onResize, append([]byte(nil), t.sinceClear...)
			t.resize = nil
			t.mu.Unlock()
			if hook != nil {
				hook(size[0], size[1], redraw)
			}
			t.mu.Lock()
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

// noteOutput keeps sinceClear: the output after the last full clear, bounded.
// Caller holds t.mu.
func (t *CaptureTransport) noteOutput(data []byte) {
	if locs := captureFullClear.FindAllIndex(data, -1); len(locs) > 0 {
		t.sinceClear = append(t.sinceClear[:0], data[locs[len(locs)-1][0]:]...)
	} else {
		t.sinceClear = append(t.sinceClear, data...)
	}
	if over := len(t.sinceClear) - captureReplayMax; over > 0 {
		t.sinceClear = bytes.Clone(t.sinceClear[over:])
	}
}
