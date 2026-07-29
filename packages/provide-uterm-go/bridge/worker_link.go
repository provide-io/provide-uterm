//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/coder/websocket"
	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
)

// Port of provide.uterm.server.bridge.worker_link.TermBridge — the worker-side
// WebSocket client that connects to the manager/hub endpoint
// /ws/worker/{worker_id}/term and speaks the inline DLE/STX control channel.
//
// It forwards live terminal output (worker → hub → browsers), answers snapshot
// requests, and applies hijack control + input from the hub. Beyond the Python
// worker_link it also drives the worker-side handshake this Go port
// consolidates: it emits a worker_hello carrying the protocol range +
// input_mode + capabilities on connect, optionally an inline heartbeat, and a
// resume-token lifecycle (send resume on connect when a token is held, capture
// session_token frames from the hub).
//
// Deviations from the Python module:
//   - All socket writes go through one send goroutine reading a single queue.
//     Snapshot/status/hello frames are enqueued rather than written to the
//     socket directly, because coder/websocket requires serialized writes; the
//     Python code writes snapshots straight to the socket.
//   - Terminal output is carried byte-faithfully as latin-1 via
//     controlchannel.WSBytesToChannelStr, matching the Go control-channel /
//     emulator pipeline (CP437 decoding happens once inside the emulator). The
//     Python worker_link decodes raw bytes as CP437 into the term data string.

// WatchFunc is the screen-update callback a Session invokes: snapshot is the
// latest screen state, raw is the new raw output bytes.
type WatchFunc func(snapshot map[string]any, raw []byte)

// Session is the minimal worker terminal-session interface TermBridge needs.
// Port of the WorkerSession protocol.
type Session interface {
	// AddWatch registers a callback invoked on each screen update.
	AddWatch(fn WatchFunc)
	// Send delivers keystrokes to the remote terminal.
	Send(ctx context.Context, data string) error
	// SetSize resizes the terminal.
	SetSize(ctx context.Context, cols, rows int) error
	// Snapshot returns the current emulator snapshot, or nil when unavailable.
	Snapshot() map[string]any
}

// Worker is the minimal worker interface TermBridge needs. Port of the Worker
// protocol.
type Worker interface {
	// Session returns the active session, or nil when disconnected.
	Session() Session
	// SetHijacked pauses (true) or resumes (false) automation.
	SetHijacked(ctx context.Context, enabled bool) error
	// RequestStep allows one loop iteration while hijacked.
	RequestStep(ctx context.Context) error
}

// MessageHandler is an app-specific control-message handler registered via
// RegisterMessageHandler. It receives the full decoded control message.
type MessageHandler func(ctx context.Context, msg map[string]any) error

// Config configures a TermBridge.
type Config struct {
	// Worker is the object driven by hub control frames (required).
	Worker Worker
	// WorkerID is the unique identifier used in the WebSocket URL (required).
	WorkerID string
	// ManagerURL is the base manager/hub URL (http:// or https://; required).
	ManagerURL string
	// InputMode is advertised in the worker_hello ("open" default, or
	// "hijack").
	InputMode string
	// MaxWSMessageBytes bounds a single WS message (floored at 1024, default
	// 1 MiB).
	MaxWSMessageBytes int
	// Capabilities, when non-empty, is advertised in the worker_hello.
	Capabilities map[string]any
	// HeartbeatInterval, when > 0, enables an inline heartbeat frame at that
	// cadence.
	HeartbeatInterval time.Duration
	// ResumeToken, when non-empty, is sent as a resume frame on every connect.
	ResumeToken string
	// Encoding selects how raw terminal bytes become the term-frame string:
	// "cp437" (default — matches the Python worker_link, which decodes BBS
	// output to Unicode box-drawing before framing) or "latin-1" (the
	// byte-faithful shim).
	Encoding string
	// DialTimeout bounds a single connection attempt (default 30s).
	DialTimeout time.Duration
	// BearerToken, when non-empty, is sent as "Authorization: Bearer <token>"
	// on the worker handshake. A hub configured with a worker token closes the
	// socket 1008 without it (Python passes the same header from
	// HostedSessionRuntime._run).
	BearerToken string
	// Logger is the structured logger; nil falls back to the telemetry logger.
	Logger *slog.Logger
}

// defaultReconnectBackoff mirrors the Python _RECONNECT_BACKOFF tuple.
var defaultReconnectBackoff = []time.Duration{
	1 * time.Second, 2 * time.Second, 5 * time.Second, 10 * time.Second, 30 * time.Second,
}

// TermBridge is the worker-side WebSocket bridge to the hub.
type TermBridge struct {
	worker            Worker
	workerID          string
	managerURL        string
	inputMode         string
	maxWSMessageBytes int
	capabilities      map[string]any
	encoding          string
	heartbeatInterval time.Duration
	dialTimeout       time.Duration
	bearerToken       string
	logger            *slog.Logger

	// reconnectBackoff is the backoff schedule (overridable in tests).
	reconnectBackoff []time.Duration

	sendQ chan queuedFrame

	mu              sync.Mutex
	running         bool
	cancel          context.CancelFunc
	done            chan struct{}
	attachedSession Session
	latestSnapshot  map[string]any
	resumeToken     string
	customHandlers  map[string]MessageHandler
}

// queuedFrame is one pending outbound message. isTerm selects raw terminal data
// (encoded via EncodeTerminalData); otherwise control carries a control-frame
// payload (encoded via EncodeControlFrame). Port of _encode_bridge_frame's two
// branches.
type queuedFrame struct {
	isTerm  bool
	data    string
	control map[string]any
}

// New builds a TermBridge from cfg.
func New(cfg Config) *TermBridge {
	logger := cfg.Logger
	if logger == nil {
		logger = ptel.GetLogger(context.Background(), "provide.uterm.bridge.worker_link")
	}
	maxBytes := cfg.MaxWSMessageBytes
	if maxBytes < 1024 {
		maxBytes = 1_048_576
	}
	mode := cfg.InputMode
	if mode == "" {
		mode = "open"
	}
	dialTimeout := cfg.DialTimeout
	if dialTimeout <= 0 {
		dialTimeout = 30 * time.Second
	}
	encoding := cfg.Encoding
	if encoding == "" {
		encoding = "cp437"
	}
	return &TermBridge{
		worker:            cfg.Worker,
		workerID:          cfg.WorkerID,
		managerURL:        cfg.ManagerURL,
		inputMode:         mode,
		maxWSMessageBytes: maxBytes,
		capabilities:      cfg.Capabilities,
		encoding:          encoding,
		heartbeatInterval: cfg.HeartbeatInterval,
		dialTimeout:       dialTimeout,
		bearerToken:       cfg.BearerToken,
		logger:            logger,
		reconnectBackoff:  defaultReconnectBackoff,
		sendQ:             make(chan queuedFrame, 2000),
		resumeToken:       cfg.ResumeToken,
		customHandlers:    map[string]MessageHandler{},
	}
}

// encodeTermBytes converts raw terminal bytes to the term-frame string using
// the configured encoding. The Python worker_link decodes with
// encoding="cp437" (errors="replace") by default, so downstream consumers see
// Unicode box-drawing; "latin-1" keeps the byte-faithful shim.
func (b *TermBridge) encodeTermBytes(raw []byte) string {
	if b.encoding == "latin-1" {
		return controlchannel.WSBytesToChannelStr(raw)
	}
	return screen.DecodeCP437(raw)
}

// RegisterMessageHandler registers an app-specific control-message handler.
// It is invoked for control messages whose "type" is not handled by the
// built-in dispatch (snapshot_req/control/resize/...). Re-registering a type
// replaces the prior handler. Port of register_message_handler.
func (b *TermBridge) RegisterMessageHandler(messageType string, handler MessageHandler) {
	b.mu.Lock()
	b.customHandlers[messageType] = handler
	b.mu.Unlock()
}

// ResumeToken returns the resume token currently held (captured from the hub or
// seeded via Config).
func (b *TermBridge) ResumeToken() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.resumeToken
}

// toWSURL converts a manager base URL to its ws:// / wss:// form and appends
// path. Port of _to_ws_url.
func toWSURL(managerURL, path string) string {
	base := strings.TrimRight(managerURL, "/")
	switch {
	case strings.HasPrefix(base, "https://"):
		base = "wss://" + base[len("https://"):]
	case strings.HasPrefix(base, "http://"):
		base = "ws://" + base[len("http://"):]
	}
	return base + path
}

// AttachSession registers a watch on the worker's current session to forward
// terminal output. Idempotent. Port of attach_session.
func (b *TermBridge) AttachSession() {
	session := b.worker.Session()
	b.mu.Lock()
	if session == nil || b.attachedSession == session {
		b.mu.Unlock()
		return
	}
	b.attachedSession = session
	b.mu.Unlock()

	session.AddWatch(func(snapshot map[string]any, raw []byte) {
		b.mu.Lock()
		b.latestSnapshot = snapshot
		b.mu.Unlock()
		if len(raw) == 0 {
			return
		}
		data := b.encodeTermBytes(raw)
		if !b.enqueue(queuedFrame{isTerm: true, data: data}) {
			b.logger.Debug("term_bridge_drop", "worker_id", b.workerID, "reason", "queue_full")
		}
	})
}

// enqueue offers a frame to the send queue, dropping it (returning false) when
// the queue is full. Mirrors the Python put_nowait + QueueFull drop.
func (b *TermBridge) enqueue(f queuedFrame) bool {
	select {
	case b.sendQ <- f:
		return true
	default:
		return false
	}
}

// Start launches the reconnecting bridge goroutine. Idempotent. Port of start.
func (b *TermBridge) Start(ctx context.Context) {
	b.mu.Lock()
	if b.running {
		b.mu.Unlock()
		return
	}
	b.running = true
	runCtx, cancel := context.WithCancel(ctx)
	b.cancel = cancel
	done := make(chan struct{})
	b.done = done
	b.mu.Unlock()

	go func() {
		defer close(done)
		b.run(runCtx)
	}()
}

// Stop signals the bridge to stop and waits for it to clean up. Port of stop.
func (b *TermBridge) Stop() {
	b.mu.Lock()
	b.running = false
	cancel := b.cancel
	done := b.done
	b.mu.Unlock()
	if cancel != nil {
		cancel()
	}
	if done != nil {
		<-done
	}
}

func (b *TermBridge) isRunning() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.running
}

func (b *TermBridge) setRunning(v bool) {
	b.mu.Lock()
	b.running = v
	b.mu.Unlock()
}

// run is the reconnect loop. Port of _run.
func (b *TermBridge) run(ctx context.Context) {
	wsURL := toWSURL(b.managerURL, "/ws/worker/"+b.workerID+"/term")
	b.logger.Info("term_bridge_connecting", "worker_id", b.workerID, "url", wsURL)

	attempt := 0
	for b.isRunning() {
		status, permanentURL := b.dialAndServe(ctx, wsURL, &attempt)
		// A cancelled context (Stop) ends the loop; the top-of-loop isRunning()
		// re-check handles a stop signalled without cancellation.
		if ctx.Err() != nil {
			return
		}
		if (status != 0 || permanentURL) && b.handlePermanentError(status, permanentURL) {
			break
		}
		delay := b.reconnectBackoff[min(attempt, len(b.reconnectBackoff)-1)]
		attempt++
		timer := time.NewTimer(delay)
		select {
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return
		}
	}
}

// dialAndServe dials once and, on success, serves the connection until it
// closes. It returns the failed-handshake HTTP status (0 when none) and whether
// the URL is permanently malformed, so run can decide on backoff vs. give-up.
// On a successful dial it resets *attempt to 0.
func (b *TermBridge) dialAndServe(ctx context.Context, wsURL string, attempt *int) (status int, permanentURL bool) {
	dialCtx, cancel := context.WithTimeout(ctx, b.dialTimeout)
	defer cancel()
	conn, resp, err := websocket.Dial(dialCtx, wsURL, b.dialOptions())
	if err != nil {
		if resp != nil {
			status = resp.StatusCode
		}
		permanentURL = isMalformedWSURL(wsURL)
		b.logger.Warn("term_bridge_disconnected", "worker_id", b.workerID, "error", err.Error(), "attempt", *attempt)
		return status, permanentURL
	}
	*attempt = 0
	conn.SetReadLimit(int64(b.maxWSMessageBytes))
	b.serveConnection(ctx, conn)
	return 0, false
}

// dialOptions carries the worker bearer token on the handshake, or nil when
// none is configured (the coder/websocket default).
func (b *TermBridge) dialOptions() *websocket.DialOptions {
	if b.bearerToken == "" {
		return nil
	}
	return &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": []string{"Bearer " + b.bearerToken}},
	}
}

// isMalformedWSURL reports whether wsURL cannot be a WebSocket URL and so a
// retry can never succeed (mirrors the Python InvalidURI permanent-error path).
func isMalformedWSURL(wsURL string) bool {
	u, err := url.Parse(wsURL)
	if err != nil {
		return true
	}
	return u.Scheme != "ws" && u.Scheme != "wss"
}

// handlePermanentError reports whether a dial failure is permanent (auth
// rejection or malformed URL), stopping the reconnect loop. Port of
// _handle_permanent_error.
func (b *TermBridge) handlePermanentError(status int, permanentURL bool) bool {
	if status == http.StatusUnauthorized || status == http.StatusForbidden || status == http.StatusNotFound {
		b.logger.Error("term_bridge_permanent_error", "worker_id", b.workerID, "status", status)
		b.setRunning(false)
		return true
	}
	if permanentURL {
		b.logger.Error("term_bridge_permanent_error", "worker_id", b.workerID, "reason", "malformed_url")
		b.setRunning(false)
		return true
	}
	return false
}

// serveConnection runs one connection lifetime: emit the handshake, then run
// the send + recv (+ optional heartbeat) goroutines until any of them exits.
// Port of _handle_connection.
func (b *TermBridge) serveConnection(ctx context.Context, conn *websocket.Conn) {
	connCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer func() { _ = conn.CloseNow() }()

	b.AttachSession()
	b.enqueueHello()
	if token := b.ResumeToken(); token != "" {
		b.enqueue(queuedFrame{control: map[string]any{"type": "resume", "token": token}})
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); b.sendLoop(connCtx, cancel, conn) }()
	go func() { defer wg.Done(); b.recvLoop(connCtx, cancel, conn) }()
	if b.heartbeatInterval > 0 {
		wg.Add(1)
		go func() { defer wg.Done(); b.heartbeatLoop(connCtx, cancel) }()
	}
	wg.Wait()
}
