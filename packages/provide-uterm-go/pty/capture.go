//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"

	ptel "github.com/provide-io/provide-telemetry/go"
)

// Capture frame channel identifiers. Port of capture.py CHANNEL_* constants.
// A reader ignores a channel it does not know, so a producer may send a newer
// one to an older reader.
const (
	ChannelStdout  = 0x01
	ChannelStdin   = 0x02
	ChannelConnect = 0x03
	// ChannelStats is the shim's own writer counters, as an ASCII key=value
	// line. The Go connector does not report them; it ignores the channel.
	ChannelStats = 0x04
	// ChannelGeometry is the terminal's size, from whoever owns the pty (a
	// supervisor), as >HH: cols, rows. The LD_PRELOAD shim never sends it: it
	// cannot see a resize.
	ChannelGeometry = 0x05
	// ChannelInput is the producer's keystroke-socket path, as a UTF-8 absolute
	// path. An empty payload withdraws the path. Sent only by a producer that
	// can deliver keystrokes (a supervisor) — never the LD_PRELOAD shim. A
	// reader that predates it ignores it, as it does any channel it does not know.
	ChannelInput = 0x06
)

// geometryPayloadSize, geometryMin and geometryMax bound a geometry payload:
// exactly four bytes, each side 1..1000 inclusive. Port of capture.py GEOMETRY,
// GEOMETRY_MIN and GEOMETRY_MAX.
const (
	geometryPayloadSize = 4
	geometryMin         = 1
	geometryMax         = 1000
	// inputMaxLen bounds a keystroke-socket path payload. Real paths are at
	// most the Unix socket address limit; 4096 matches the spec's over-length
	// vector. Port of capture.py INPUT_MAX_LEN.
	inputMaxLen = 4096
)

// ParseGeometry reads (cols, rows) from a geometry payload. ok is false for
// anything malformed, which the caller ignores: the frame's own length kept the
// stream in step, so only this frame is untrustworthy. Port of
// capture.parse_geometry.
func ParseGeometry(payload []byte) (cols, rows int, ok bool) {
	if len(payload) != geometryPayloadSize {
		return 0, 0, false
	}
	cols = int(binary.BigEndian.Uint16(payload[0:2]))
	rows = int(binary.BigEndian.Uint16(payload[2:4]))
	if cols < geometryMin || cols > geometryMax || rows < geometryMin || rows > geometryMax {
		return 0, 0, false
	}
	return cols, rows, true
}

// ParseInputPath reads the keystroke-socket path from an input payload. Port of
// capture.parse_input_path.
//
// An empty payload withdraws the path (path is "", ok is true). A non-empty
// payload is a UTF-8 absolute path. Non-UTF-8, a path over inputMaxLen, or a
// path that is not absolute returns ok=false — the caller ignores the frame.
//
// The returned path is NOT validated against the capture-socket convention
// (.sock → .stdin.sock); that is the connector's job, since it depends on the
// capture socket the session was created with.
func ParseInputPath(payload []byte) (path string, ok bool) {
	if len(payload) > inputMaxLen {
		return "", false
	}
	if len(payload) == 0 {
		return "", true
	}
	path = string(payload)
	if !utf8.ValidString(path) {
		return "", false
	}
	if !strings.HasPrefix(path, "/") {
		return "", false
	}
	return path, true
}

const (
	// headerSize is the framing header width: 1B channel + 4B big-endian length.
	headerSize = 5
	// queueMaxSize bounds buffered capture frames (drop-oldest past this). Port
	// of capture.py _QUEUE_MAXSIZE.
	queueMaxSize = 4096
	// maxFrameBytes caps a single frame's payload (framing-violation past this).
	// Port of capture.py _MAX_FRAME_BYTES.
	maxFrameBytes = 16 * 1024 * 1024 // 16 MiB
)

// The permissions a listening capture socket may have. Connecting to a Unix
// socket needs write permission on it, so these say who may produce into it,
// and so read and type into the captured terminal. Owner-only is the default.
// Owner and group is for a server that runs as a user of its own while the
// producers (the shim, a session supervisor) run as another: the group comes
// from a setgid directory, so nothing here chowns. No other mode is accepted,
// since a world bit would hand the terminal to every local user. Port of
// capture.py OWNER_ONLY_SOCKET_MODE / OWNER_AND_GROUP_SOCKET_MODE.
const (
	OwnerOnlySocketMode     = 0o600
	OwnerAndGroupSocketMode = 0o660
)

// CheckSocketMode is mode as an int, if it is one a capture socket may be
// bound with. It takes any integer a config map holds: an int from the server,
// an int64 from TOML, or a whole float64 from JSON, which has no other number.
// Anything else is refused rather than read as a number. Port of
// capture.check_socket_mode, in its words.
func CheckSocketMode(mode any) (int, error) {
	var m int
	switch t := mode.(type) {
	case int:
		m = t
	case int64:
		m = int(t)
	case float64:
		// Whole and inside int64, or it is not an integer: int() of ±Inf,
		// NaN or a float past int64 is implementation-defined, and Python
		// calls every float "not an integer" anyway. -2^63 is exact as a
		// float64; 2^63 is the first float past the top.
		if t != math.Trunc(t) || t < math.MinInt64 || t >= -math.MinInt64 {
			return 0, errors.New("capture socket mode must be an integer: 0o600 or 0o660")
		}
		m = int(t)
	default:
		return 0, errors.New("capture socket mode must be an integer: 0o600 or 0o660")
	}
	if m != OwnerOnlySocketMode && m != OwnerAndGroupSocketMode {
		return 0, fmt.Errorf("capture socket mode must be 0o600 or 0o660, got: %s", pyOct(m))
	}
	return m, nil
}

// pyOct is v as Python's oct() writes it: the sign ahead of the 0o prefix.
func pyOct(v int) string {
	digits := strconv.FormatInt(int64(v), 8)
	if digits, negative := strings.CutPrefix(digits, "-"); negative {
		return "-0o" + digits
	}
	return "0o" + digits
}

// captureBindUmask is the umask that leaves exactly mode's bits on a file it
// creates (0o177 for 0o600). Port of capture._bind_umask.
func captureBindUmask(mode int) int {
	return 0o777 &^ mode
}

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
	mode     int
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

// NewCaptureSocket builds a CaptureSocket bound (on Start) to socketPath,
// owner-only.
func NewCaptureSocket(socketPath string) (*CaptureSocket, error) {
	return newCaptureSocketWithQueue(socketPath, OwnerOnlySocketMode, queueMaxSize)
}

// NewCaptureSocketWithMode is NewCaptureSocket bound with mode instead, which
// must be OwnerOnlySocketMode or OwnerAndGroupSocketMode.
func NewCaptureSocketWithMode(socketPath string, mode int) (*CaptureSocket, error) {
	return newCaptureSocketWithQueue(socketPath, mode, queueMaxSize)
}

// newCaptureSocketWithQueue is the queue-size-parameterized constructor used by
// tests to force overflow deterministically.
func newCaptureSocketWithQueue(socketPath string, mode, queueCap int) (*CaptureSocket, error) {
	if err := ValidateSocketPath(socketPath); err != nil {
		return nil, err
	}
	if _, err := CheckSocketMode(mode); err != nil {
		return nil, err
	}
	return &CaptureSocket{
		path:     socketPath,
		mode:     mode,
		queueCap: queueCap,
		conns:    make(map[net.Conn]struct{}),
		queue:    make(chan CaptureFrame, queueCap),
	}, nil
}

// Path returns the socket path.
func (s *CaptureSocket) Path() string { return s.path }

// Start binds the listening socket with its mode and begins accepting. Port of
// CaptureSocket.start.
func (s *CaptureSocket) Start() error {
	// A capture session re-created after a crash binds the path its last
	// holder never unbound.
	removeStaleSocket(s.path)
	// Set the umask that leaves exactly the mode's bits *before* bind so the
	// socket is created with them atomically — a post-bind chmod would leave a
	// window where the socket exists with default-umask perms that any local
	// user could connect to.
	ln, err := listenUnderUmask(s.path, captureBindUmask(s.mode))
	if err != nil {
		return err
	}
	// Belt-and-suspenders: enforce the mode even if the platform ignored the umask.
	if err := chmodSocket(s.path, os.FileMode(s.mode)); err != nil {
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
