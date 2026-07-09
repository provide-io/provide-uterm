//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package termsession combines a transport with the terminal emulator to
// provide ready-to-use Session-protocol objects. Port of
// provide.uterm.transport_session, telnet_session, and ws_session.
package termsession

import (
	"context"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/emulator"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/transports"
)

// SendEncoding selects the codec Send uses for outgoing strings.
type SendEncoding string

// Send encodings.
const (
	// EncodingUTF8 sends the string's UTF-8 bytes (the default).
	EncodingUTF8 SendEncoding = "utf-8"
	// EncodingCP437 encodes to CP437 with '?' replacement — the high-byte /
	// ANSI convention BBS servers expect on the wire.
	EncodingCP437 SendEncoding = "cp437"
)

// WatchFunc receives each raw byte chunk read from the wire, IAC-stripped
// and ANSI/CP437-intact, BEFORE the emulator consumes it. The state map is
// currently always empty. Callbacks must not block.
type WatchFunc func(state map[string]any, raw []byte)

// TransportSession owns the background reader loop, the screen-change
// sequence counter, the raw-byte watcher fan-out, and the connect/close
// lifecycle over any transports.ConnectionTransport plus a TerminalEmulator.
// It satisfies session.Session, session.ExpectSession, and
// session.ConnectionChecker.
type TransportSession struct {
	transport    transports.ConnectionTransport
	cols, rows   int
	sendEncoding SendEncoding
	// connectTransport is the single transport-specific hook (the Python
	// _connect_transport override point).
	connectTransport func(ctx context.Context) error

	emu *emulator.TerminalEmulator

	mu        sync.Mutex
	connected bool
	changeSeq int
	updateCh  chan struct{}
	watchers  []WatchFunc

	readerDone chan struct{}
	readerStop context.CancelFunc
}

// Options configure a TransportSession.
type Options struct {
	// Cols/Rows default to 80×25.
	Cols, Rows int
	// SendEncoding defaults to EncodingUTF8.
	SendEncoding SendEncoding
}

// New wraps transport with terminal emulation. connect is the
// transport-specific dial hook invoked by Connect.
func New(transport transports.ConnectionTransport, connect func(ctx context.Context) error, opts Options) *TransportSession {
	if opts.Cols <= 0 {
		opts.Cols = 80
	}
	if opts.Rows <= 0 {
		opts.Rows = 25
	}
	if opts.SendEncoding == "" {
		opts.SendEncoding = EncodingUTF8
	}
	return &TransportSession{
		transport:        transport,
		cols:             opts.Cols,
		rows:             opts.Rows,
		sendEncoding:     opts.SendEncoding,
		connectTransport: connect,
		emu:              emulator.New(opts.Cols, opts.Rows, ""),
		updateCh:         make(chan struct{}),
	}
}

// Connect opens the transport connection and starts the background reader.
func (s *TransportSession) Connect(ctx context.Context) error {
	if err := s.connectTransport(ctx); err != nil {
		return err
	}
	readerCtx, cancel := context.WithCancel(context.Background())
	s.mu.Lock()
	s.connected = true
	s.readerStop = cancel
	s.readerDone = make(chan struct{})
	done := s.readerDone
	s.mu.Unlock()
	go s.readerLoop(readerCtx, done)
	return nil
}

// Close stops the background reader and closes the connection.
func (s *TransportSession) Close(ctx context.Context) error {
	s.mu.Lock()
	s.connected = false
	stop, done := s.readerStop, s.readerDone
	s.readerStop, s.readerDone = nil, nil
	s.mu.Unlock()
	if stop != nil {
		stop()
		<-done
	}
	return s.transport.Disconnect(ctx)
}

// Snapshot returns the current emulated screen state.
func (s *TransportSession) Snapshot() session.Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.emu.GetSnapshot()
}

// ANSIScreen returns the current screen as ANSI-styled text (with SGR
// colors) for live renderers — Snapshot returns plain text only.
func (s *TransportSession) ANSIScreen() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.emu.ANSIScreen()
}

// Send writes a string to the server, encoded with the configured codec
// (unrepresentable characters never raise: CP437 replaces with '?').
func (s *TransportSession) Send(ctx context.Context, data string) error {
	var payload []byte
	if s.sendEncoding == EncodingCP437 {
		payload = screen.EncodeCP437(data)
	} else {
		payload = []byte(data)
	}
	return s.transport.Send(ctx, payload)
}

// SendExpect sends keys and waits for expected terminal output.
func (s *TransportSession) SendExpect(ctx context.Context, keys string, opts session.ExpectOptions) (session.ExpectResult, error) {
	return session.SendAndExpect(ctx, s, keys, opts)
}

// WaitForUpdate blocks until new bytes arrive from the server or the timeout
// elapses.
func (s *TransportSession) WaitForUpdate(ctx context.Context, timeout time.Duration) (bool, error) {
	s.mu.Lock()
	ch := s.updateCh
	s.mu.Unlock()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-ch:
		return true, nil
	case <-timer.C:
		return false, nil
	case <-ctx.Done():
		return false, ctx.Err()
	}
}

// IsConnected reports whether the session is connected.
func (s *TransportSession) IsConnected() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.connected
}

// ScreenChangeSeq returns a monotonic counter that increments on each screen
// update. Capture it before sending input, then pass it to
// WaitForScreenChange to avoid reading stale screen data.
func (s *TransportSession) ScreenChangeSeq() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.changeSeq
}

// WaitForScreenChange blocks until the screen updates beyond since (pass a
// negative since to wait for any next update), or the timeout elapses.
func (s *TransportSession) WaitForScreenChange(ctx context.Context, timeout time.Duration, since int) (bool, error) {
	deadline := time.Now().Add(timeout)
	for {
		s.mu.Lock()
		seq := s.changeSeq
		ch := s.updateCh
		s.mu.Unlock()
		if since >= 0 && seq > since {
			return true, nil
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return false, nil
		}
		timer := time.NewTimer(remaining)
		select {
		case <-ch:
			timer.Stop()
			if since < 0 {
				return true, nil
			}
		case <-timer.C:
			s.mu.Lock()
			changed := s.changeSeq > max(since, 0)
			s.mu.Unlock()
			return changed, nil
		case <-ctx.Done():
			timer.Stop()
			return false, ctx.Err()
		}
	}
}

// AddWatch registers a callback fired with each raw byte chunk read from the
// wire — the supported tap for raw terminal bytes (ANSI SGR and CP437 high
// bytes intact, before the emulator absorbs them).
func (s *TransportSession) AddWatch(callback WatchFunc) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.watchers = append(s.watchers, callback)
}

// Emulator exposes the underlying terminal emulator.
func (s *TransportSession) Emulator() *emulator.TerminalEmulator {
	return s.emu
}

// readerLoop reads from the transport (IAC-stripped) and feeds the emulator,
// fanning raw chunks to watchers first so they see wire content rather than
// the decoded display.
func (s *TransportSession) readerLoop(ctx context.Context, done chan<- struct{}) {
	defer close(done)
	for {
		// Close flips connected before cancelling; a cancelled ctx also
		// surfaces as a Receive error on the real transports.
		s.mu.Lock()
		connected := s.connected
		s.mu.Unlock()
		if !connected {
			return
		}
		data, err := s.transport.Receive(ctx, 4096, 500*time.Millisecond)
		if err != nil {
			s.mu.Lock()
			s.connected = false
			s.mu.Unlock()
			return
		}
		if len(data) == 0 {
			continue
		}
		s.mu.Lock()
		watchers := make([]WatchFunc, len(s.watchers))
		copy(watchers, s.watchers)
		s.mu.Unlock()
		for _, cb := range watchers {
			func() {
				defer func() { _ = recover() }() // watcher panics must not kill the reader
				cb(map[string]any{}, data)
			}()
		}
		s.mu.Lock()
		s.emu.Process(data)
		s.changeSeq++
		close(s.updateCh)
		s.updateCh = make(chan struct{})
		s.mu.Unlock()
	}
}
