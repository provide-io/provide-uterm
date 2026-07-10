//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strconv"
	"sync"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"
)

const (
	// notifyMaxLine bounds a single notify line (runaway-sender guard). Port of
	// pam_listener.py _MAX_LINE.
	notifyMaxLine = 4096
	// notifyReadTimeout bounds a single readline. Port of the wait_for(5.0).
	notifyReadTimeout = 5 * time.Second
	// notifySocketMode / notifyBindUmask: owner-only notify socket.
	notifySocketMode = 0o600
	notifyBindUmask  = 0o177
)

// PamEvent is one notification received from pam_uterm.so. Port of
// pam_listener.PamEvent.
type PamEvent struct {
	Event         string // "open" | "close"
	Username      string
	TTY           string
	PID           int
	Mode          string // "notify" | "capture"
	CaptureSocket string // set when Mode == "capture"
	Timestamp     float64
}

// PamEventHandler is called for every successfully parsed event. Handler panics
// are recovered so one bad event never kills the listener.
type PamEventHandler func(ctx context.Context, ev PamEvent)

// PamNotifyListener is an async Unix-domain socket server for pam_uterm.so
// notifications (newline-delimited JSON). Port of
// pam_listener.PamNotifyListener.
//
// requirePeerUIDs is an opt-in allowlist of peer euids that may connect; nil
// means no enforcement (the euid is still logged). On platforms without
// SO_PEERCRED (e.g. macOS) the check is skipped (warn + allow), matching Python.
type PamNotifyListener struct {
	path            string
	requirePeerUIDs []int
	handler         PamEventHandler

	mu       sync.Mutex
	listener net.Listener
	conns    map[net.Conn]struct{}
	started  bool
	closed   bool
	wg       sync.WaitGroup
}

// NewPamNotifyListener builds a listener bound (on Start) to socketPath.
func NewPamNotifyListener(socketPath string, requirePeerUIDs []int) (*PamNotifyListener, error) {
	if err := ValidateSocketPath(socketPath); err != nil {
		return nil, err
	}
	return &PamNotifyListener{
		path:            socketPath,
		requirePeerUIDs: requirePeerUIDs,
		conns:           make(map[net.Conn]struct{}),
	}, nil
}

// SocketPath returns the socket path. Port of the socket_path property.
func (l *PamNotifyListener) SocketPath() string { return l.path }

// Start binds the notify socket (owner-only) and begins accepting; handler is
// invoked for each parsed event. Port of PamNotifyListener.start.
func (l *PamNotifyListener) Start(ctx context.Context, handler PamEventHandler) error {
	l.mu.Lock()
	if l.started {
		l.mu.Unlock()
		return fmt.Errorf("PamNotifyListener already started")
	}
	l.mu.Unlock()

	l.unlinkStaleSocket()
	// Bind under a restrictive umask so the socket is created 0o600 atomically.
	prev := umaskSet(notifyBindUmask)
	ln, err := net.Listen("unix", l.path)
	umaskSet(prev)
	if err != nil {
		return err
	}
	if err := os.Chmod(l.path, notifySocketMode); err != nil {
		_ = ln.Close()
		return err
	}

	l.mu.Lock()
	l.handler = handler
	l.listener = ln
	l.started = true
	l.mu.Unlock()

	l.wg.Add(1)
	go l.acceptLoop(ctx, ln)
	ptel.GetLogger(ctx, "provide.uterm.pty").Info("pam_notify_listener started", "socket", l.path)
	return nil
}

// unlinkStaleSocket removes a leftover socket file at our path before binding,
// but only when it is actually a socket. Port of _unlink_stale_socket.
func (l *PamNotifyListener) unlinkStaleSocket() {
	info, err := os.Stat(l.path)
	if err != nil {
		return
	}
	if info.Mode()&os.ModeSocket != 0 {
		_ = os.Remove(l.path)
	}
}

func (l *PamNotifyListener) acceptLoop(ctx context.Context, ln net.Listener) {
	defer l.wg.Done()
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		l.mu.Lock()
		if l.closed {
			l.mu.Unlock()
			_ = conn.Close()
			return
		}
		l.conns[conn] = struct{}{}
		l.mu.Unlock()

		l.wg.Add(1)
		go func() {
			defer l.wg.Done()
			defer l.removeConn(conn)
			l.handleConn(ctx, conn)
		}()
	}
}

func (l *PamNotifyListener) removeConn(conn net.Conn) {
	l.mu.Lock()
	delete(l.conns, conn)
	l.mu.Unlock()
	_ = conn.Close()
}

// handleConn authenticates the peer (SO_PEERCRED) then reads newline-delimited
// events. Port of PamNotifyListener._handle_connection.
func (l *PamNotifyListener) handleConn(ctx context.Context, conn net.Conn) {
	log := ptel.GetLogger(ctx, "provide.uterm.pty")
	euid, ok := peerEUID(conn)
	if !ok {
		log.Warn("pam_notify peer auth unavailable on this platform; relying on socket permissions")
	} else {
		log.Debug("pam_notify peer euid", "euid", euid)
		if l.requirePeerUIDs != nil && !containsInt(l.requirePeerUIDs, euid) {
			log.Warn("pam_notify rejected connection", "euid", euid, "allowlist", l.requirePeerUIDs)
			return
		}
	}

	reader := bufio.NewReaderSize(conn, notifyMaxLine)
	for {
		_ = conn.SetReadDeadline(time.Now().Add(notifyReadTimeout))
		line, err := reader.ReadSlice('\n')
		if err == bufio.ErrBufferFull {
			// Oversized line: discard to the next newline and skip it.
			discardToNewline(conn, reader)
			log.Warn("pam_notify_listener oversized_line — dropped")
			continue
		}
		if err != nil {
			return
		}
		if len(line) > notifyMaxLine {
			log.Warn("pam_notify_listener oversized_line — dropped", "bytes", len(line))
			continue
		}
		ev, ok := parseEvent(ctx, line)
		if !ok {
			continue
		}
		if l.handler != nil {
			l.dispatch(ctx, ev)
		}
	}
}

// dispatch invokes the handler, recovering panics so one bad event never kills
// the listener. Port of the try/except around await handler(event).
func (l *PamNotifyListener) dispatch(ctx context.Context, ev PamEvent) {
	defer func() {
		if r := recover(); r != nil {
			ptel.GetLogger(ctx, "provide.uterm.pty").Error(
				"pam_notify_listener handler error", "event", ev.Event, "username", ev.Username, "panic", r)
		}
	}()
	l.handler(ctx, ev)
}

// discardToNewline consumes bytes until a newline (or error) to recover from an
// oversized line without unbounded buffering.
func discardToNewline(conn net.Conn, reader *bufio.Reader) {
	for {
		_ = conn.SetReadDeadline(time.Now().Add(notifyReadTimeout))
		if _, err := reader.ReadSlice('\n'); err != bufio.ErrBufferFull {
			return
		}
	}
}

// Stop shuts down the server and removes the socket file. Port of
// PamNotifyListener.stop.
func (l *PamNotifyListener) Stop(ctx context.Context) error {
	l.mu.Lock()
	if !l.started || l.closed {
		l.mu.Unlock()
		return nil
	}
	l.closed = true
	ln := l.listener
	l.listener = nil
	conns := make([]net.Conn, 0, len(l.conns))
	for c := range l.conns {
		conns = append(conns, c)
	}
	l.mu.Unlock()

	if ln != nil {
		_ = ln.Close()
	}
	for _, c := range conns {
		_ = c.Close()
	}
	l.wg.Wait()

	if err := os.Remove(l.path); err != nil && !os.IsNotExist(err) {
		return err
	}
	ptel.GetLogger(ctx, "provide.uterm.pty").Info("pam_notify_listener stopped", "socket", l.path)
	return nil
}

// parseEvent parses one JSON line into a PamEvent, returning ok=false on any
// error. Port of pam_listener._parse_event.
func parseEvent(ctx context.Context, line []byte) (PamEvent, bool) {
	log := ptel.GetLogger(ctx, "provide.uterm.pty")
	var data map[string]any
	if err := json.Unmarshal(line, &data); err != nil {
		log.Warn("pam_notify_listener bad_json")
		return PamEvent{}, false
	}

	ev, _ := data["event"].(string)
	if ev != "open" && ev != "close" {
		log.Warn("pam_notify_listener unknown_event", "event", data["event"])
		return PamEvent{}, false
	}

	username := stringOrEmpty(data["username"])
	tty := stringOrEmpty(data["tty"])
	pid := intOrZero(data["pid"])

	if username == "" {
		log.Warn("pam_notify_listener missing username — dropped")
		return PamEvent{}, false
	}

	mode := "notify"
	if stringOrEmpty(data["mode"]) == "capture" {
		mode = "capture"
	}
	captureSocket := ""
	if s := stringOrEmpty(data["capture_socket"]); s != "" {
		captureSocket = s
	}

	return PamEvent{
		Event:         ev,
		Username:      username,
		TTY:           tty,
		PID:           pid,
		Mode:          mode,
		CaptureSocket: captureSocket,
		Timestamp:     nowTS(),
	}, true
}

// stringOrEmpty coerces a JSON value to a string ("" for nil / non-string),
// mirroring str(data.get(key) or "").
func stringOrEmpty(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	default:
		return fmt.Sprintf("%v", t)
	}
}

// intOrZero coerces a JSON value to an int (0 on failure), mirroring
// int(data.get("pid") or 0).
func intOrZero(v any) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case int:
		return t
	case string:
		if n, err := strconv.Atoi(t); err == nil {
			return n
		}
	}
	return 0
}

func containsInt(s []int, x int) bool {
	for _, v := range s {
		if v == x {
			return true
		}
	}
	return false
}
