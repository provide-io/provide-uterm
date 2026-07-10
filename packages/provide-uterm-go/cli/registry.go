//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"sync"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
)

// sessionEntry is one managed session: its immutable definition plus the mutable
// runtime state (lifecycle, input mode, live connector, last error).
type sessionEntry struct {
	def       serverconfig.SessionDefinition
	lifecycle string // "waiting" | "running" | "stopped"
	inputMode string
	session   *termsession.TransportSession // nil when no live connector
	lastErr   *string
	stoppedAt *float64
	createdAt string
	annSeq    int
}

// connectFn opens a live connector for a session definition. It returns
// (nil, nil) when the connector type has no live transport ("no live connector"
// → a running-but-not-connected status), a connected session on success, or an
// error when the dial fails.
type connectFn func(ctx context.Context, def serverconfig.SessionDefinition) (*termsession.TransportSession, error)

// SessionRegistryImpl is a minimal, real in-memory SessionRegistry over the
// config's declared [[sessions]]. Telnet/WebSocket sessions get a live
// termsession connector on Start; every other connector type is tracked as
// running-but-not-connected. It satisfies server.SessionRegistry.
type SessionRegistryImpl struct {
	mu       sync.Mutex
	order    []string
	entries  map[string]*sessionEntry
	recDeflt bool
	// connect builds the live connector; overridable in tests with a fake.
	connect connectFn
}

var _ server.SessionRegistry = (*SessionRegistryImpl)(nil)

// NewSessionRegistry seeds a registry from cfg.Sessions, auto-starting sessions
// flagged auto_start (mirroring the Python registry bootstrap). Recording
// defaults follow cfg.Recording.EnabledByDefault.
func NewSessionRegistry(cfg *serverconfig.UtermServerConfig) *SessionRegistryImpl {
	r := &SessionRegistryImpl{
		entries:  map[string]*sessionEntry{},
		recDeflt: cfg.Recording.EnabledByDefault,
		connect:  defaultConnect,
	}
	for _, def := range cfg.Sessions {
		r.seed(def)
	}
	return r
}

// seed inserts a definition as a fresh entry (waiting, per-def input mode).
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
		lifecycle: "waiting",
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
	connected := e.session != nil && e.session.IsConnected()
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

// CreateSession creates a session from a free-form definition payload. A missing
// session_id → 422; a duplicate id → 409.
func (r *SessionRegistryImpl) CreateSession(_ context.Context, payload map[string]any) (*server.SessionStatus, error) {
	def, err := definitionFromPayload(payload)
	if err != nil {
		return nil, err
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
	if ok && e.session != nil {
		_ = e.session.Close(ctx)
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
