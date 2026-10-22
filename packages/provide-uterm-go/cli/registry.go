//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"cmp"
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/annotation"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// sessionEntry is one managed session: its immutable definition plus the mutable
// runtime state (lifecycle, input mode, live connector, last error).
type sessionEntry struct {
	def serverconfig.SessionDefinition
	// lifecycle is the wire runtime state; the names are declared once in
	// server.SessionLifecycleStates, so this cannot drift into a vocabulary of
	// its own.
	lifecycle server.SessionLifecycleState
	inputMode string
	conn      connectors.Connector // nil when no live connector
	// bridge is the worker-side link that attaches this session to the hub
	// (nil when no hub is wired, or the session is stopped). See
	// registry_worker.go.
	bridge    *bridge.TermBridge
	lastErr   *string
	stoppedAt *float64
	createdAt string
	annSeq    int
	// recorder records the session's worker connections (nil until the
	// session first starts with a recording store wired). Kept across
	// restarts, as the reference keeps its runtime. See session_recording.go.
	recorder *sessionRecorder
	// keeper keeps a PAM capture session's screen in the capture-state store
	// (nil for any other session, or with no store). See capture_keeper.go.
	keeper *captureKeeper
}

// connectFn opens a live connector for a session definition. It returns
// (nil, nil) when the connector type has no live transport ("no live connector"
// → a running-but-not-connected status), a started connector on success, or an
// error when the build/dial fails.
type connectFn func(ctx context.Context, def serverconfig.SessionDefinition) (connectors.Connector, error)

// SessionRegistryImpl is a real in-memory SessionRegistry over the config's
// declared [[sessions]], backed by the connectors package. Each session gets a
// real connector (shell PTY / ssh / telnet / websocket) started on demand, so
// `uterm server` hosts live terminals rather than not-connected stubs. It
// satisfies server.SessionRegistry.
type SessionRegistryImpl struct {
	mu      sync.Mutex
	order   []string
	entries map[string]*sessionEntry
	// retiring holds ids whose session is being torn down, or whose leftover
	// recording is being retired before a new session takes them. Held until
	// the old session's recording is closed (its last flush written) and then
	// retired, so a new session under the id can neither be served the old
	// recording nor receive the old one's late writes. Port of the
	// reference's registry._retiring.
	retiring map[string]chan struct{}
	recDeflt bool
	// recCfg is the recording section sessions record under, and recording
	// the store they record into (nil: nothing is recorded). Wired by
	// SetRecording; see session_recording.go.
	recCfg    serverconfig.RecordingConfig
	recording recording.Store
	detector  *annotation.PatternDetector
	// capture is where PAM capture sessions are kept across a restart and
	// after they end ([session_persistence]); nil keeps none. Wired by
	// SetCaptureState.
	capture *capturestate.Store
	// connect builds the live connector; overridable in tests with a fake.
	connect connectFn
	// egress is the SSRF / connector-target guard; blockPrivate carries
	// security.block_private_connector_targets. Together they enforce the egress
	// policy at the CreateSession chokepoint (port of assert_session_egress_allowed).
	egress       *server.EgressGuard
	blockPrivate bool
	// captureSocketMode is pam.capture_socket_mode: the permissions every
	// capture session's socket is bound with, however the session was made.
	captureSocketMode int
	// eventBus is the hub EventBus for long-poll events/watch (optional).
	eventBus *hub.EventBus
	// hub, managerURL and workerToken are the hub link a started session
	// attaches itself to; bridgeCtx bounds the worker bridges' lifetime. All
	// four are wired once by SetHubLink (registry_worker.go) and are nil/empty
	// in tests that drive the registry standalone.
	hub         *hub.TermHub
	managerURL  string
	workerToken string
	bridgeCtx   context.Context
}

var _ server.SessionRegistry = (*SessionRegistryImpl)(nil)

// NewSessionRegistry seeds a registry from cfg.Sessions; every session starts
// stopped — configured, never brought up. Sessions flagged auto_start are
// spawned separately once the server is listening, via StartAutoStartSessions
// (mirroring the Python lifespan boot task) — construction has no connector
// side effects. Recording defaults follow cfg.Recording.EnabledByDefault.
func NewSessionRegistry(cfg *serverconfig.UtermServerConfig) *SessionRegistryImpl {
	r := &SessionRegistryImpl{
		entries:      map[string]*sessionEntry{},
		retiring:     map[string]chan struct{}{},
		recDeflt:     cfg.Recording.EnabledByDefault,
		recCfg:       cfg.Recording,
		egress:       server.NewEgressGuard(nil, nil),
		blockPrivate: cfg.Security.BlockPrivateConnectorTargets,
		// A config built in code rather than loaded leaves it zero, which is
		// the reference's default: owner-only.
		captureSocketMode: cmp.Or(cfg.Pam.CaptureSocketMode, uptty.OwnerOnlySocketMode),
	}
	r.connect = func(ctx context.Context, def serverconfig.SessionDefinition) (connectors.Connector, error) {
		return connectPrepared(ctx, withCaptureSocketMode(def, r.captureSocketMode), r.prepareCapture)
	}
	for _, def := range cfg.Sessions {
		r.seed(def)
	}
	return r
}

// SetEventBus wires the hub EventBus so WatchSessionEvents can long-poll.
// Safe to call once after NewSessionRegistry (server boot).
func (r *SessionRegistryImpl) SetEventBus(bus *hub.EventBus) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.eventBus = bus
}

// EventBus returns the wired EventBus, or nil.
func (r *SessionRegistryImpl) EventBus() *hub.EventBus {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.eventBus
}

// seed inserts a definition as a fresh entry (stopped, per-def input mode).
func (r *SessionRegistryImpl) seed(def serverconfig.SessionDefinition) {
	if _, ok := r.entries[def.SessionID]; ok {
		return
	}
	created := def.CreatedAt
	if created.IsZero() {
		created = time.Now().UTC()
	}
	r.order = append(r.order, def.SessionID)
	r.entries[def.SessionID] = &sessionEntry{
		def:       def,
		lifecycle: server.LifecycleStopped,
		inputMode: def.InputMode,
		createdAt: created.UTC().Format(time.RFC3339),
	}
}

// recordingEnabled resolves the per-session recording flag against the default.
func (r *SessionRegistryImpl) recordingEnabled(def serverconfig.SessionDefinition) bool {
	if def.RecordingEnabled != nil {
		return *def.RecordingEnabled
	}
	return r.recDeflt
}

// snapshotStatus builds the wire status for an entry (caller holds r.mu).
func (r *SessionRegistryImpl) snapshotStatus(e *sessionEntry) *server.SessionStatus {
	connected := e.conn != nil && e.conn.IsConnected()
	tags := e.def.Tags
	if tags == nil {
		tags = []string{}
	}
	return &server.SessionStatus{
		SessionID:        e.def.SessionID,
		DisplayName:      e.def.DisplayName,
		CreatedAt:        e.createdAt,
		ConnectorType:    e.def.ConnectorType,
		LifecycleState:   e.lifecycle,
		InputMode:        e.inputMode,
		Connected:        connected,
		AutoStart:        e.def.AutoStart,
		Tags:             tags,
		RecordingEnabled: r.recordingEnabled(e.def),
		// As the reference reports it: available whenever enabled — provided
		// there is a store to record into.
		RecordingAvailable: r.recording != nil && r.recordingEnabled(e.def),
		Owner:              e.def.Owner,
		Visibility:         e.def.Visibility,
		StoppedAt:          e.stoppedAt,
		LastError:          e.lastErr,
	}
}

// GetDefinition returns a session's definition; ok=false when unknown.
func (r *SessionRegistryImpl) GetDefinition(_ context.Context, id string) (*serverconfig.SessionDefinition, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return nil, false
	}
	def := e.def
	return &def, true
}

// ListWithDefinitions returns every session's (status, definition) pair in
// declaration order.
func (r *SessionRegistryImpl) ListWithDefinitions(context.Context) []server.SessionListItem {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]server.SessionListItem, 0, len(r.order))
	for _, id := range r.order {
		e := r.entries[id]
		def := e.def
		out = append(out, server.SessionListItem{Status: r.snapshotStatus(e), Definition: &def})
	}
	return out
}

// GetSession returns a session's status, or ErrSessionNotFound.
func (r *SessionRegistryImpl) GetSession(_ context.Context, id string) (*server.SessionStatus, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return nil, server.ErrSessionNotFound
	}
	return r.snapshotStatus(e), nil
}

// CreateSession creates a session from a free-form (caller-supplied) definition
// payload, egress-guarding the connector target. A missing session_id → 422; a
// duplicate id → 409.
func (r *SessionRegistryImpl) CreateSession(ctx context.Context, payload map[string]any) (*server.SessionStatus, error) {
	return r.createSession(ctx, payload, true)
}

// CreateSessionInternal creates a server-minted session WITHOUT the connector-
// target egress check — for inbound tunnel placeholders whose connector_config
// (tunnel_type, no dial-out url) is not caller-controlled and never dialed. Port
// of create_session(validate_connector_target=False) (routes/tunnels.py). Never
// wire this to a user-supplied connector config.
func (r *SessionRegistryImpl) CreateSessionInternal(ctx context.Context, payload map[string]any) (*server.SessionStatus, error) {
	return r.createSession(ctx, payload, false)
}

// createSession is the shared create core; validateEgress gates the SSRF
// chokepoint (block cloud-metadata targets always, private targets when
// security.block_private_connector_targets is set; returns *server.
// EgressBlockedError → HTTP 422).
func (r *SessionRegistryImpl) createSession(
	ctx context.Context, payload map[string]any, validateEgress bool,
) (*server.SessionStatus, error) {
	def, err := definitionFromPayload(payload)
	if err != nil {
		return nil, err
	}
	if validateEgress {
		if err := r.egress.AssertSessionEgressAllowed(ctx, def.ConnectorType, def.ConnectorConfig, r.blockPrivate); err != nil {
			return nil, err
		}
	}
	store, err := r.reserveForCreate(ctx, def.SessionID)
	if err != nil {
		return nil, err
	}
	// Released however this returns, a panic included; normally below, in
	// the same step as the insert.
	released := false
	defer func() {
		if !released {
			r.mu.Lock()
			r.releaseLocked(def.SessionID)
			r.mu.Unlock()
		}
	}()
	// A recording left under this id (a session from before a restart, or one
	// a store kept) is not this new session's: retire it before the session
	// becomes visible, so nothing can read it as this one's.
	retireErr := retireIn(store, def.SessionID)
	var flush func()
	// Deferred first, so it runs once r.mu is released: no disk under the lock.
	defer func() {
		if flush != nil {
			flush()
		}
	}()
	r.mu.Lock()
	defer r.mu.Unlock()
	// Released and inserted in one step: no other create can slip in.
	r.releaseLocked(def.SessionID)
	released = true
	if retireErr != nil {
		return nil, retireErr
	}
	r.seed(def)
	e := r.entries[def.SessionID]
	flush = r.setDefinitionLocked(e)
	return r.snapshotStatus(e), nil
}

// reservedIDWait is how long a create waits for a teardown holding its id.
// Port of _RESERVED_ID_WAIT_S.
//
// Both waits are computed by a function, not a bare `15 * time.Second`
// initializer: a package-level initializer carries no coverage counter, so a
// mutation of it is permanently NOT_COVERED (see egressResolveTimeout).
var reservedIDWait = defaultReservedIDWait()

func defaultReservedIDWait() time.Duration { return 15 * time.Second }

// removalStopTimeout is how long DeleteSession waits for the session to stop
// before it retires the recording and releases the id regardless. Port of
// _REMOVAL_STOP_TIMEOUT_S.
var removalStopTimeout = defaultRemovalStopTimeout()

func defaultRemovalStopTimeout() time.Duration { return 10 * time.Second }

// registryLogger is where stopRemoved logs; swappable in tests.
var registryLogger = func(ctx context.Context) *slog.Logger {
	return ptel.GetLogger(ctx, "provide.uterm.server.registry")
}

// reserveForCreate reserves id for a create, waiting out a teardown (or
// another create) that holds it for up to reservedIDWait: a tty-named PAM id
// is reused by the next login on that tty, and the relay retries an open, so
// the id is often wanted again while its last holder is still going. Then the
// usual checks run, so a session that now exists is still "already exists";
// only a teardown that outlasts the wait is "session is being deleted".
// Returns the recording store to retire into. Port of _reserve_for_create.
func (r *SessionRegistryImpl) reserveForCreate(ctx context.Context, id string) (recording.Store, error) {
	timer := time.NewTimer(reservedIDWait)
	defer timer.Stop()
	for {
		r.mu.Lock()
		if _, exists := r.entries[id]; exists {
			r.mu.Unlock()
			return nil, &server.SessionConflictError{Msg: "session already exists: " + id}
		}
		pending, held := r.retiring[id]
		if !held {
			r.retiring[id] = make(chan struct{})
			store := r.recording
			r.mu.Unlock()
			return store, nil
		}
		r.mu.Unlock()
		select {
		case <-pending:
		case <-timer.C:
			return nil, &server.SessionConflictError{Msg: "session is being deleted: " + id}
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}

// releaseLocked releases a reserved id and wakes whatever waits on it.
// Caller holds r.mu.
func (r *SessionRegistryImpl) releaseLocked(id string) {
	if ch, ok := r.retiring[id]; ok {
		delete(r.retiring, id)
		close(ch)
	}
}

// retireIn retires id's recording in store; a nil store records nothing.
func retireIn(store recording.Store, id string) error {
	if store == nil {
		return nil
	}
	return recording.Retire(store, id)
}

// UpdateSession applies a partial update (display_name, input_mode, visibility,
// tags). Unknown id → 404.
func (r *SessionRegistryImpl) UpdateSession(_ context.Context, id string, payload map[string]any) (*server.SessionStatus, error) {
	var flush func()
	// Deferred first, so it runs once r.mu is released: no disk under the lock.
	defer func() {
		if flush != nil {
			flush()
		}
	}()
	r.mu.Lock()
	defer r.mu.Unlock()
	e, ok := r.entries[id]
	if !ok {
		return nil, server.ErrSessionNotFound
	}
	if v, ok := payload["display_name"].(string); ok {
		e.def.DisplayName = v
	}
	if v, ok := payload["input_mode"].(string); ok {
		if v != "hijack" && v != "open" {
			return nil, &server.SessionValidationError{Msg: "invalid input_mode: " + v}
		}
		e.inputMode = v
		e.def.InputMode = v
	}
	if v, ok := payload["visibility"].(string); ok {
		e.def.Visibility = v
	}
	if v, ok := payload["tags"].([]string); ok {
		e.def.Tags = v
	}
	flush = r.setDefinitionLocked(e)
	return r.snapshotStatus(e), nil
}

// DeleteSession removes a session (idempotent), detaching its worker bridge and
// stopping any live connector.
func (r *SessionRegistryImpl) DeleteSession(ctx context.Context, id string) error {
	r.mu.Lock()
	if _, held := r.retiring[id]; held {
		// Already being torn down by another call; that call retires it.
		r.mu.Unlock()
		return nil
	}
	r.retiring[id] = make(chan struct{})
	// Released however this returns, a panic in the teardown included. A panic
	// while r.mu is still held (in takeBridge, say) must not re-lock it.
	locked := true
	defer func() {
		if !locked {
			r.mu.Lock()
		}
		r.releaseLocked(id)
		r.mu.Unlock()
	}()
	store := r.recording
	e, ok := r.entries[id]
	var br *bridge.TermBridge
	var conn connectors.Connector
	var rec *sessionRecorder
	var keeper *captureKeeper
	if ok {
		br = takeBridge(e)
		conn, rec, keeper = e.conn, e.recorder, e.keeper
		delete(r.entries, id)
		for i, oid := range r.order {
			if oid == id {
				r.order = append(r.order[:i], r.order[i+1:]...)
				break
			}
		}
	}
	capture := r.capture
	locked = false
	r.mu.Unlock()
	stopRemoved(ctx, id, br, rec, keeper, conn)
	if capture != nil {
		// After the stop, whose last screen this keeps.
		capture.End(id)
	}
	// Only now, with the recording closed and its last writes in it (or the
	// stop given up on), is it retired; the deferred release frees the id.
	return retireIn(store, id)
}

// stopRemoved stops a deleted session, waiting at most removalStopTimeout:
// a stop that hangs must not hold the id forever, so past the timeout it is
// logged and left running, and the delete goes on to retire and release. A
// panic in it is recovered and logged for the same reason.
func stopRemoved(
	ctx context.Context, id string, br *bridge.TermBridge, rec *sessionRecorder, keeper *captureKeeper,
	conn connectors.Connector,
) {
	logger := registryLogger(ctx)
	timeout := removalStopTimeout // read once: the goroutine may outlive this call
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer func() {
			if p := recover(); p != nil {
				logger.Warn("session_removal_stop_panicked", "session_id", id, "panic", fmt.Sprint(p))
			}
		}()
		// Same order as StopSession: detach from the hub before the connector
		// goes, or a worker whose socket outlived its terminal keeps
		// reconnecting against a session id the registry can no longer list.
		stopBridge(br)
		settleRecording(rec)
		saveLastScreen(keeper)
		if conn != nil {
			stopCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), timeout)
			defer cancel()
			_ = conn.Stop(stopCtx)
		}
	}()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-done:
	case <-timer.C:
		logger.Warn("session_removal_stop_timed_out", "session_id", id, "timeout_s", timeout.Seconds())
		// The stop is left running; whatever it still writes must not reach
		// the store once the recording is retired and the id released.
		if rec != nil {
			rec.retire()
		}
	}
}

// definitionFromPayload builds a SessionDefinition from a create payload,
// applying the same required/enum rules the Python model enforces.
func definitionFromPayload(payload map[string]any) (serverconfig.SessionDefinition, error) {
	id, _ := payload["session_id"].(string)
	if id == "" {
		return serverconfig.SessionDefinition{}, &server.SessionValidationError{Msg: "session_id is required"}
	}
	if !sessionIDValid(id) {
		return serverconfig.SessionDefinition{}, &server.SessionValidationError{Msg: "session_id must match ^[\\w\\-]+$"}
	}
	if len(id) > serverconfig.MaxSessionIDBytes {
		return serverconfig.SessionDefinition{}, &server.SessionValidationError{
			Msg: fmt.Sprintf("session_id must be at most %d bytes (UTF-8)", serverconfig.MaxSessionIDBytes),
		}
	}
	connType, _ := payload["connector_type"].(string)
	if connType == "" {
		connType = "shell"
	}
	inputMode, _ := payload["input_mode"].(string)
	if inputMode == "" {
		inputMode = "open"
	}
	visibility, _ := payload["visibility"].(string)
	if visibility == "" {
		visibility = "public"
	}
	display, _ := payload["display_name"].(string)
	if display == "" {
		display = id
	}
	cc, _ := payload["connector_config"].(map[string]any)
	if cc == nil {
		cc = map[string]any{}
	}
	def := serverconfig.SessionDefinition{
		SessionID:       id,
		DisplayName:     display,
		ConnectorType:   connType,
		ConnectorConfig: cc,
		InputMode:       inputMode,
		Visibility:      visibility,
		Tags:            []string{},
		CreatedAt:       time.Now().UTC(),
		// The model's defaults, which a kept capture session's file carries
		// and its reader requires.
		AutoTransferIdleS: 30,
		KeystrokeQueue:    "display",
	}
	// Per-session override of recording.enabled_by_default; absent means
	// "use the default", as the reference's recording_enabled: bool | None.
	if v, ok := payload["recording_enabled"].(bool); ok {
		def.RecordingEnabled = &v
	}
	return def, nil
}

// sessionIDValid mirrors the ^[\w\-]+$ constraint without pulling in the
// unexported serverconfig validator.
func sessionIDValid(id string) bool {
	if id == "" {
		return false
	}
	for _, c := range id {
		// The accepted classes are one boolean statement rather than a switch
		// with an empty arm: Go's cover tool instruments case BODIES, not case
		// expressions, so in switch form every comparison here lands outside any
		// covered block and the mutation gate reports it NOT COVERED —
		// unkillable by any test. As an assignment the comparisons sit inside a
		// covered block, so the boundary mutants are reachable and killed by
		// TestSessionIDValidCharsetBoundaries.
		allowed := c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-'
		if !allowed {
			return false
		}
	}
	return true
}
