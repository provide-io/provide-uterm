//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"os"
	"sync"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// Capture frame channel identifiers. Port of capture.py CHANNEL_* constants.
const (
	ChannelStdout  = 0x01
	ChannelStdin   = 0x02
	ChannelConnect = 0x03
)

const (
	// headerSize is the framing header width: 1B channel + 4B big-endian length.
	headerSize = 5
	// queueMaxSize bounds buffered capture frames (drop-oldest past this). Port
	// of capture.py _QUEUE_MAXSIZE.
	queueMaxSize = 4096
	// maxFrameBytes caps a single frame's payload (framing-violation past this).
	// Port of capture.py _MAX_FRAME_BYTES.
	maxFrameBytes = 16 * 1024 * 1024 // 16 MiB
	// socketMode is the owner-only permission for the listening socket.
	socketMode = 0o600
	// bindUmask yields 0o600 at file creation (0o777 &^ 0o177 == 0o600).
	bindUmask = 0o177
)

// CaptureFrame is one decoded capture frame. Port of capture.CaptureFrame.
type CaptureFrame struct {
	Channel int
	Data    []byte
}

// CaptureSocket is a Unix-domain socket server that receives length-prefixed
// frames from libuterm_capture. Port of capture.CaptureSocket.
//
// Wire format: [1B channel][4B length big-endian][N bytes payload].
type CaptureSocket struct {
	path     string
	queueCap int

	mu       sync.Mutex
	listener net.Listener
	conns    map[net.Conn]struct{}
	started  bool
	closed   bool
	wg       sync.WaitGroup

	// queue is a buffered channel; enqMu makes drop-oldest atomic against
	// concurrent producers so overflow deterministically discards the oldest.
	queue chan CaptureFrame
	enqMu sync.Mutex
}

// NewCaptureSocket builds a CaptureSocket bound (on Start) to socketPath.
func NewCaptureSocket(socketPath string) (*CaptureSocket, error) {
	return newCaptureSocketWithQueue(socketPath, queueMaxSize)
}

// newCaptureSocketWithQueue is the queue-size-parameterized constructor used by
// tests to force overflow deterministically.
func newCaptureSocketWithQueue(socketPath string, queueCap int) (*CaptureSocket, error) {
	if err := ValidateSocketPath(socketPath); err != nil {
		return nil, err
	}
	return &CaptureSocket{
		path:     socketPath,
		queueCap: queueCap,
		conns:    make(map[net.Conn]struct{}),
		queue:    make(chan CaptureFrame, queueCap),
	}, nil
}

// Path returns the socket path.
func (s *CaptureSocket) Path() string { return s.path }

// Start binds the listening socket (owner-only) and begins accepting. Port of
// CaptureSocket.start.
func (s *CaptureSocket) Start() error {
	// Set a restrictive umask *before* bind so the socket is created 0o600
	// atomically — a post-bind chmod would leave a window where the socket
	// exists with default-umask perms that any local user could connect to.
	prev := umaskSet(bindUmask)
	ln, err := net.Listen("unix", s.path)
	umaskSet(prev)
	if err != nil {
		return err
	}
	// Belt-and-suspenders: enforce 0o600 even if the platform ignored the umask.
	if err := os.Chmod(s.path, socketMode); err != nil {
		_ = ln.Close()
		return err
	}
	s.mu.Lock()
	s.listener = ln
	s.started = true
	s.mu.Unlock()

	s.wg.Add(1)
	go s.acceptLoop(ln)
	return nil
}

// acceptLoop accepts connections until the listener is closed.
func (s *CaptureSocket) acceptLoop(ln net.Listener) {
	defer s.wg.Done()
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			_ = conn.Close()
			return
		}
		s.conns[conn] = struct{}{}
		s.mu.Unlock()

		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			defer s.removeConn(conn)
			s.handleConn(conn)
		}()
	}
}

func (s *CaptureSocket) removeConn(conn net.Conn) {
	s.mu.Lock()
	delete(s.conns, conn)
	s.mu.Unlock()
	_ = conn.Close()
}

// handleConn reads framed data from r until EOF or a framing violation. It is
// separated from the net.Conn so the framing logic is unit-testable with any
// io.Reader. Port of CaptureSocket._handle_connection's read loop.
func (s *CaptureSocket) handleConn(r io.Reader) {
	header := make([]byte, headerSize)
	for {
		if _, err := io.ReadFull(r, header); err != nil {
			return
		}
		channel := int(header[0])
		length := binary.BigEndian.Uint32(header[1:])
		if length > maxFrameBytes {
			// Hostile/corrupt producer: refuse to allocate for one frame. Drop
			// the connection rather than read the body — checking BEFORE the
			// body read is what prevents the OOM.
			ptel.GetLogger(context.Background(), "provide.uterm.pty").Warn(
				"capture_frame_too_large", "length", length, "cap", maxFrameBytes)
			return
		}
		data := make([]byte, length)
		if _, err := io.ReadFull(r, data); err != nil {
			return
		}
		s.enqueue(CaptureFrame{Channel: channel, Data: data})
	}
}

// enqueue buffers frame, applying drop-oldest backpressure when the queue is
// full. Port of CaptureSocket._enqueue.
func (s *CaptureSocket) enqueue(frame CaptureFrame) {
	s.enqMu.Lock()
	defer s.enqMu.Unlock()
	select {
	case s.queue <- frame:
		return
	default:
	}
	// Full: drop the oldest, then enqueue.
	select {
	case <-s.queue:
	default:
	}
	s.queue <- frame
	ptel.GetLogger(context.Background(), "provide.uterm.pty").Warn(
		"capture_backpressure_drop_oldest", "maxsize", s.queueCap)
}

// ReadFrame blocks until a frame is available or ctx is done. Port of
// CaptureSocket.read_frame.
func (s *CaptureSocket) ReadFrame(ctx context.Context) (CaptureFrame, error) {
	select {
	case f := <-s.queue:
		return f, nil
	case <-ctx.Done():
		return CaptureFrame{}, ctx.Err()
	}
}

// ReadNowait returns the next buffered frame and true, or false when empty.
// Port of CaptureSocket.read_nowait.
func (s *CaptureSocket) ReadNowait() (CaptureFrame, bool) {
	select {
	case f := <-s.queue:
		return f, true
	default:
		return CaptureFrame{}, false
	}
}

// QueueLen returns the number of buffered frames (test/observability helper).
func (s *CaptureSocket) QueueLen() int { return len(s.queue) }

// Stop closes the listener + open connections and removes the socket file.
// Idempotent. Port of CaptureSocket.stop.
func (s *CaptureSocket) Stop() error {
	s.mu.Lock()
	if !s.started || s.closed {
		// stop() before start() — or a second stop() — is a no-op and must not
		// remove any file (matches CaptureSocket.stop's `if server is None`).
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	ln := s.listener
	s.listener = nil
	conns := make([]net.Conn, 0, len(s.conns))
	for c := range s.conns {
		conns = append(conns, c)
	}
	s.mu.Unlock()

	if ln != nil {
		_ = ln.Close()
	}
	for _, c := range conns {
		_ = c.Close()
	}
	s.wg.Wait()

	if err := os.Remove(s.path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}
