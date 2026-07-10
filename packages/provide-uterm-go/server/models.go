//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"errors"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// SessionStatus is the wire shape returned by every session create/get/patch/
// control endpoint — the Go port of the Python SessionRuntimeStatus.model_dump.
// Nullable fields are pointers WITHOUT omitempty so they serialize as JSON
// null (matching model_dump, which always emits the key).
type SessionStatus struct {
	SessionID          string   `json:"session_id"`
	DisplayName        string   `json:"display_name"`
	CreatedAt          string   `json:"created_at"` // ISO-8601
	ConnectorType      string   `json:"connector_type"`
	LifecycleState     string   `json:"lifecycle_state"`
	InputMode          string   `json:"input_mode"`
	Connected          bool     `json:"connected"`
	AutoStart          bool     `json:"auto_start"`
	Tags               []string `json:"tags"`
	RecordingEnabled   bool     `json:"recording_enabled"`
	RecordingAvailable bool     `json:"recording_available"`
	Owner              *string  `json:"owner"`
	Visibility         string   `json:"visibility"`
	StoppedAt          *float64 `json:"stopped_at"`
	LastError          *string  `json:"last_error"`
}

// SessionListItem pairs a session's runtime status with its definition so the
// list handler can both authorize (per-definition) and serialize (the status).
type SessionListItem struct {
	Status     *SessionStatus
	Definition *serverconfig.SessionDefinition
}

// WatchParams configures a session event long-poll (events/watch).
type WatchParams struct {
	TimeoutMS  int
	EventTypes []string
	Pattern    string
	MaxEvents  int
}

// Annotation is the payload of a session annotation (session.annotate).
type Annotation struct {
	Label       string
	Description string
	Severity    string
	Principal   string
}

// SessionValidationError maps to HTTP 422 — a session-definition payload failed
// validation (Python SessionValidationError).
type SessionValidationError struct{ Msg string }

func (e *SessionValidationError) Error() string { return e.Msg }

// SessionConflictError maps to HTTP 409 — e.g. a duplicate session id (Python
// raises a bare ValueError which the route turns into 409).
type SessionConflictError struct{ Msg string }

func (e *SessionConflictError) Error() string { return e.Msg }

// EgressBlockedError maps to HTTP 422 — a quick-connect target was blocked by
// the private-connector-target egress guard.
type EgressBlockedError struct{ Msg string }

func (e *EgressBlockedError) Error() string { return e.Msg }

// ErrSessionNotFound maps to HTTP 404 — the analogue of registry KeyError.
var ErrSessionNotFound = errors.New("session not found")

// ErrNoRuntime maps to HTTP 404 for annotate — no active runtime for a session.
var ErrNoRuntime = errors.New("no active runtime")

// SessionRegistry is the session-management surface the REST routes depend on.
// It is intentionally an interface: the concrete SessionRegistry (connectors,
// recording, control-plane persistence) is not part of this HTTP layer and is
// supplied by the CLI. Methods mirror the Python SessionRegistry the routes
// call (see routes/sessions.py). Opaque payloads (analysis, snapshot, events,
// recording metadata) are passed through as generic JSON.
type SessionRegistry interface {
	// GetDefinition returns a session's definition; ok=false when unknown.
	GetDefinition(ctx context.Context, sessionID string) (*serverconfig.SessionDefinition, bool)
	// ListWithDefinitions returns every session's (status, definition) pair.
	ListWithDefinitions(ctx context.Context) []SessionListItem
	// GetSession returns a session's status, or ErrSessionNotFound.
	GetSession(ctx context.Context, sessionID string) (*SessionStatus, error)
	// CreateSession creates a session from a free-form definition payload.
	// Returns *SessionValidationError (422) or *SessionConflictError (409).
	CreateSession(ctx context.Context, payload map[string]any) (*SessionStatus, error)
	// UpdateSession applies a partial update. *SessionValidationError → 422,
	// ErrSessionNotFound → 404.
	UpdateSession(ctx context.Context, sessionID string, payload map[string]any) (*SessionStatus, error)
	// DeleteSession removes a session (idempotent).
	DeleteSession(ctx context.Context, sessionID string) error
	// StartSession / StopSession / RestartSession drive the connector lifecycle;
	// ErrSessionNotFound → 404.
	StartSession(ctx context.Context, sessionID string) (*SessionStatus, error)
	StopSession(ctx context.Context, sessionID string) (*SessionStatus, error)
	RestartSession(ctx context.Context, sessionID string) (*SessionStatus, error)
	// SetMode switches a session's input mode. ErrSessionNotFound → 404.
	SetMode(ctx context.Context, sessionID, mode string) (*SessionStatus, error)
	// ClearSession clears a session's screen. ErrSessionNotFound → 404.
	ClearSession(ctx context.Context, sessionID string) (*SessionStatus, error)
	// AnalyzeSession returns an opaque analysis object. ErrSessionNotFound → 404.
	AnalyzeSession(ctx context.Context, sessionID string) (map[string]any, error)
	// LastSnapshot returns the latest snapshot dict, or nil when absent.
	LastSnapshot(ctx context.Context, sessionID string) (map[string]any, error)
	// Events returns up to limit recent events.
	Events(ctx context.Context, sessionID string, limit int) ([]map[string]any, error)
	// WatchSessionEvents long-polls for events.
	WatchSessionEvents(ctx context.Context, sessionID string, p WatchParams) (map[string]any, error)
	// AnnotateSession records an operator annotation, returning (ts, seq).
	// ErrNoRuntime → 404.
	AnnotateSession(ctx context.Context, sessionID string, ann Annotation) (float64, int, error)
}

// ProfileStore is the connection-profile persistence surface the profiles
// routes depend on. *serverconfig.FileProfileStore satisfies it.
type ProfileStore interface {
	ListProfiles(owner *string) ([]serverconfig.ConnectionProfile, error)
	GetProfile(profileID string) (*serverconfig.ConnectionProfile, error)
	CreateProfile(profile serverconfig.ConnectionProfile) (*serverconfig.ConnectionProfile, error)
	UpdateProfile(profileID string, updates map[string]any) (*serverconfig.ConnectionProfile, error)
	DeleteProfile(profileID string) (bool, error)
}
