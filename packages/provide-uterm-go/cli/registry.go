//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
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
	mu       sync.Mutex
	order    []string
	entries  map[string]*sessionEntry
	recDeflt bool
	// connect builds the live connector; overridable in tests with a fake.
	connect connectFn
	// egress is the SSRF / connector-target guard; blockPrivate carries
	// security.block_private_connector_targets. Together they enforce the egress
	// policy at the CreateSession chokepoint (port of assert_session_egress_allowed).
	egress       *server.EgressGuard
	blockPrivate bool
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
		recDeflt:     cfg.Recording.EnabledByDefault,
		connect:      defaultConnect,
		egress:       server.NewEgressGuard(nil, nil),
		blockPrivate: cfg.Security.BlockPrivateConnectorTargets,
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
		SessionID:          e.def.SessionID,
		DisplayName:        e.def.DisplayName,
		CreatedAt:          e.createdAt,
		ConnectorType:      e.def.ConnectorType,
		LifecycleState:     e.lifecycle,
		InputMode:          e.inputMode,
		Connected:          connected,
		AutoStart:          e.def.AutoStart,
		Tags:               tags,
		RecordingEnabled:   r.recordingEnabled(e.def),
		RecordingAvailable: false,
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
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.entries[def.SessionID]; exists {
		return nil, &server.SessionConflictError{Msg: "session already exists: " + def.SessionID}
	}
	r.seed(def)
	return r.snapshotStatus(r.entries[def.SessionID]), nil
}

// UpdateSession applies a partial update (display_name, input_mode, visibility,
// tags). Unknown id → 404.
func (r *SessionRegistryImpl) UpdateSession(_ context.Context, id string, payload map[string]any) (*server.SessionStatus, error) {
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
	return r.snapshotStatus(e), nil
}

// DeleteSession removes a session (idempotent), stopping any live connector.
func (r *SessionRegistryImpl) DeleteSession(ctx context.Context, id string) error {
	r.mu.Lock()
	e, ok := r.entries[id]
	if ok {
		delete(r.entries, id)
		for i, oid := range r.order {
			if oid == id {
				r.order = append(r.order[:i], r.order[i+1:]...)
				break
			}
		}
	}
	r.mu.Unlock()
	if ok && e.conn != nil {
		_ = e.conn.Stop(ctx)
	}
	return nil
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
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '_', c == '-':
		default:
			return false
		}
	}
	return true
}
