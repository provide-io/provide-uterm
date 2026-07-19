//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package frames is a Go port of the WebSocket frame schema layer of
// provide-uterm. The single source of truth for wire shapes is the Pydantic
// module provide.uterm.bridge.schemas (packages/provide-uterm/src/provide/
// uterm/bridge/schemas.py); every struct here mirrors one model of the
// AnyFrame discriminated union (discriminator key: "type").
//
// Serialization semantics mirror Pydantic's model_dump(exclude_none=True):
// required fields are plain Go types and always serialize (including false
// booleans and zero numbers); optional (default-None) fields are pointers,
// or nil-able maps/slices/interfaces, tagged omitzero so absent values are
// omitted from the wire JSON.
//
// Extra-field policies from the Python models are honored by DecodeFrame:
//
//   - extra="forbid" (the default): unknown fields are rejected.
//   - extra="ignore" (HelloFrame): unknown fields are dropped.
//   - extra="allow" (IdentityFrame, StatusFrame, PresenceUpdateFrame,
//     PresenceSyncFrame): unknown fields round-trip via the Extra map.
//
// Deviation from Python: the server builders make_snapshot_frame,
// make_analysis_frame and make_hijack_state_frame dump with
// exclude_none=False, emitting explicit nulls for unset optionals
// (e.g. "prompt_detected": null, "raw": null, "owner": null). The Go
// marshalers uniformly use exclude_none=True semantics and omit those keys;
// consumers treat absent and null identically.
package frames

// Wire "type" discriminator literals, one per frame struct.
const (
	TypeTerm               = "term"
	TypeInput              = "input"
	TypeSnapshotReq        = "snapshot_req"
	TypeSnapshot           = "snapshot"
	TypeControl            = "control"
	TypeHijackState        = "hijack_state"
	TypeHijackRequest      = "hijack_request"
	TypeHijackRelease      = "hijack_release"
	TypeHijackStep         = "hijack_step"
	TypeWorkerConnected    = "worker_connected"
	TypeWorkerDisconnected = "worker_disconnected"
	TypeWorkerHello        = "worker_hello"
	TypeHeartbeat          = "heartbeat"
	TypeHeartbeatAck       = "heartbeat_ack"
	TypePing               = "ping"
	TypePong               = "pong"
	TypeHello              = "hello"
	TypeResume             = "resume"
	TypeIdentity           = "identity"
	TypeSessionToken       = "session_token"
	TypeResumeOk           = "resume_ok"
	TypeResumeFailed       = "resume_failed"
	TypeLinkPatterns       = "link_patterns"
	TypeAnalysis           = "analysis"
	TypeError              = "error"
	TypeStatus             = "status"
	TypeInputModeChanged   = "input_mode_changed"
	TypeApprovalPending    = "approval_pending"
	TypeApprovalResolved   = "approval_resolved"
	TypePresenceUpdate     = "presence_update"
	TypePresenceSync       = "presence_sync"
	TypePresenceLeave      = "presence_leave"
	TypeControlTransfer    = "control_transfer"
)

// IdentityFrame defaults (mirroring the Pydantic field defaults).
const (
	IdentityDefaultVersion     = 1
	IdentityDefaultFingerprint = ""
	IdentityDefaultTransport   = "ssh"
)

// ---------------------------------------------------------------------------
// Terminal data + snapshot
// ---------------------------------------------------------------------------

// TermFrame carries raw terminal output bytes from the worker to subscribers.
type TermFrame struct {
	Type string   `json:"type"`
	Data string   `json:"data"`
	TS   *float64 `json:"ts,omitzero"`
}

// InputFrame carries browser/operator input destined for the worker.
type InputFrame struct {
	Type string   `json:"type"`
	Data string   `json:"data"`
	TS   *float64 `json:"ts,omitzero"`
}

// SnapshotReqFrame is a browser-originated request for a fresh screen snapshot.
type SnapshotReqFrame struct {
	Type string   `json:"type"`
	TS   *float64 `json:"ts,omitzero"`
}

// SnapshotFrame is a worker-originated full-screen snapshot.
type SnapshotFrame struct {
	Type             string         `json:"type"`
	Screen           string         `json:"screen"`
	Cursor           map[string]int `json:"cursor,omitzero"`
	Cols             *int           `json:"cols,omitzero"`
	Rows             *int           `json:"rows,omitzero"`
	ScreenHash       *string        `json:"screen_hash,omitzero"`
	CursorAtEnd      *bool          `json:"cursor_at_end,omitzero"`
	HasTrailingSpace *bool          `json:"has_trailing_space,omitzero"`
	PromptDetected   map[string]any `json:"prompt_detected,omitzero"`
	// RawTail is a bounded rolling tail of raw decoded output (ANSI/control
	// intact) so consumers can recover content that scrolled off the 25-row
	// viewport within a single turn. Optional/backward-compatible.
	RawTail *string  `json:"raw_tail,omitzero"`
	TS      *float64 `json:"ts,omitzero"`
}

// ---------------------------------------------------------------------------
// Hijack lease lifecycle
// ---------------------------------------------------------------------------

// ControlFrame is a server-originated worker-control frame (pause/resume/step).
type ControlFrame struct {
	Type   string   `json:"type"`
	Action string   `json:"action"`
	Owner  *string  `json:"owner,omitzero"`
	LeaseS *float64 `json:"lease_s,omitzero"`
	TS     *float64 `json:"ts,omitzero"`
}

// HijackStateFrame is a broadcast lease-state update.
type HijackStateFrame struct {
	Type           string   `json:"type"`
	Hijacked       bool     `json:"hijacked"`
	Owner          *string  `json:"owner,omitzero"`
	LeaseExpiresAt *float64 `json:"lease_expires_at,omitzero"`
	InputMode      *string  `json:"input_mode,omitzero"`
}

// HijackRequestFrame is a browser-originated request to acquire the hijack lease.
type HijackRequestFrame struct {
	Type  string   `json:"type"`
	Token *string  `json:"token,omitzero"`
	TS    *float64 `json:"ts,omitzero"`
}

// HijackReleaseFrame is a browser-originated request to release the hijack lease.
type HijackReleaseFrame struct {
	Type string   `json:"type"`
	TS   *float64 `json:"ts,omitzero"`
}

// HijackStepFrame is a browser-originated single-step request.
type HijackStepFrame struct {
	Type string   `json:"type"`
	TS   *float64 `json:"ts,omitzero"`
}

// ---------------------------------------------------------------------------
// Worker presence
// ---------------------------------------------------------------------------

// WorkerConnectedFrame announces a worker connecting to the hub.
type WorkerConnectedFrame struct {
	Type     string   `json:"type"`
	WorkerID string   `json:"worker_id"`
	TS       *float64 `json:"ts,omitzero"`
}

// WorkerDisconnectedFrame announces a worker disconnecting from the hub.
type WorkerDisconnectedFrame struct {
	Type     string   `json:"type"`
	WorkerID string   `json:"worker_id"`
	TS       *float64 `json:"ts,omitzero"`
}

// WorkerHelloFrame is a worker-originated hello-frame carrying input_mode +
// capabilities.
type WorkerHelloFrame struct {
	Type string   `json:"type"`
	Mode *string  `json:"mode,omitzero"`
	TS   *float64 `json:"ts,omitzero"`
}

// ---------------------------------------------------------------------------
// Heartbeat / keepalive
// ---------------------------------------------------------------------------

// HeartbeatFrame is a browser-originated keepalive.
type HeartbeatFrame struct {
	Type string   `json:"type"`
	TS   *float64 `json:"ts,omitzero"`
}

// HeartbeatAckFrame is the server reply to a browser heartbeat — refreshes the
// lease.
type HeartbeatAckFrame struct {
	Type           string   `json:"type"`
	LeaseExpiresAt float64  `json:"lease_expires_at"`
	TS             *float64 `json:"ts,omitzero"`
}

// PingFrame is a liveness probe.
type PingFrame struct {
	Type string   `json:"type"`
	TS   *float64 `json:"ts,omitzero"`
}

// PongFrame is the reply to a PingFrame.
type PongFrame struct {
	Type string   `json:"type"`
	TS   *float64 `json:"ts,omitzero"`
}

// ---------------------------------------------------------------------------
// Hello / resume handshake
// ---------------------------------------------------------------------------

// HelloFrame is a server-originated hello-frame to the browser describing
// capabilities. The Python model is extra="ignore": unknown fields are
// silently dropped on decode (no Extra map).
type HelloFrame struct {
	Type                string         `json:"type"`
	WorkerID            *string        `json:"worker_id,omitzero"`
	CanHijack           *bool          `json:"can_hijack,omitzero"`
	Hijacked            *bool          `json:"hijacked,omitzero"`
	HijackedByMe        *bool          `json:"hijacked_by_me,omitzero"`
	WorkerOnline        *bool          `json:"worker_online,omitzero"`
	InputMode           *string        `json:"input_mode,omitzero"`
	Role                *string        `json:"role,omitzero"`
	HijackControl       *string        `json:"hijack_control,omitzero"`
	HijackStepSupported *bool          `json:"hijack_step_supported,omitzero"`
	Capabilities        map[string]any `json:"capabilities,omitzero"`
	ResumeSupported     *bool          `json:"resume_supported,omitzero"`
	McpSupported        *bool          `json:"mcp_supported,omitzero"`
	VncSupported        *bool          `json:"vnc_supported,omitzero"`
	ResumeToken         *string        `json:"resume_token,omitzero"`
	Resumed             *bool          `json:"resumed,omitzero"`
	ProtocolVersion     *int           `json:"protocol_version,omitzero"`
	// Protocol carries the range-negotiation handshake. Server hello sets
	// {"selected": N, "server_min": MIN, "server_max": MAX}; worker hello
	// sets {"min": ..., "max": ..., "preferred": ...}.
	Protocol map[string]int `json:"protocol,omitzero"`
	TS       *float64       `json:"ts,omitzero"`
}

// ResumeFrame asks the server to resume a previous session.
type ResumeFrame struct {
	Type     string `json:"type"`
	Token    string `json:"token"`
	PlayerID *int   `json:"player_id,omitzero"`
}

// IdentityFrame is the inline control-channel identity frame. The Python
// model is extra="allow": unknown fields round-trip via Extra. Version,
// Fingerprint and Transport have Pydantic defaults (1, "", "ssh") which
// NewIdentityFrame applies and DecodeFrame restores when the keys are absent.
type IdentityFrame struct {
	Type        string         `json:"type"`
	Version     int            `json:"version"`
	Subject     string         `json:"subject"`
	Fingerprint string         `json:"fingerprint"`
	Transport   string         `json:"transport"`
	Claims      map[string]any `json:"claims,omitzero"`
	Signature   *string        `json:"signature,omitzero"`
	// Extra holds unknown wire fields (extra="allow").
	Extra map[string]any `json:"-"`
}

// SessionTokenFrame hands the browser a resumable session token.
type SessionTokenFrame struct {
	Type     string `json:"type"`
	Token    string `json:"token"`
	PlayerID *int   `json:"player_id,omitzero"`
}

// ResumeOkFrame acknowledges a successful resume.
type ResumeOkFrame struct {
	Type string `json:"type"`
}

// ResumeFailedFrame reports a failed resume attempt.
type ResumeFailedFrame struct {
	Type   string  `json:"type"`
	Reason *string `json:"reason,omitzero"`
}

// LinkPatternEntry is one clickable-link pattern inside a LinkPatternsFrame.
// It is not itself a frame (no "type" discriminator). The Python field
// "class_" serializes under its alias "class". Action must be one of
// "cmd", "url", "key" or "focus".
type LinkPatternEntry struct {
	Pattern string  `json:"pattern"`
	Action  string  `json:"action"`
	ID      *string `json:"id,omitzero"`
	Flags   *string `json:"flags,omitzero"`
	// Group is int | string in the Python schema.
	Group   any     `json:"group,omitzero"`
	Payload any     `json:"payload,omitzero"`
	Hover   *string `json:"hover,omitzero"`
	// LineContains scopes the pattern: the client only runs the regex on
	// lines whose (SGR-stripped) text contains this substring.
	// Empty/absent = every line.
	LineContains *string `json:"line_contains,omitzero"`
	Class        *string `json:"class,omitzero"`
}

// LinkPatternsFrame pushes the clickable-link pattern set to the browser.
type LinkPatternsFrame struct {
	Type     string             `json:"type"`
	Patterns []LinkPatternEntry `json:"patterns"`
}

// ---------------------------------------------------------------------------
// Misc server → browser
// ---------------------------------------------------------------------------

// AnalysisFrame carries a formatted analysis blob plus its raw payload.
type AnalysisFrame struct {
	Type      string   `json:"type"`
	Formatted string   `json:"formatted"`
	Raw       any      `json:"raw,omitzero"`
	TS        *float64 `json:"ts,omitzero"`
}

// ErrorFrame reports a bridge error. Protocol-mismatch close frames carry the
// reason/min/max extras.
type ErrorFrame struct {
	Type      string  `json:"type"`
	Message   string  `json:"message"`
	Reason    *string `json:"reason,omitzero"`
	ClientMin *int    `json:"client_min,omitzero"`
	ClientMax *int    `json:"client_max,omitzero"`
	ServerMin *int    `json:"server_min,omitzero"`
	ServerMax *int    `json:"server_max,omitzero"`
}

// StatusFrame is a worker-originated status passthrough. The Python model is
// extra="allow": the type discriminator and ts are the only modelled fields;
// arbitrary status payloads round-trip via Extra.
type StatusFrame struct {
	Type string   `json:"type"`
	TS   *float64 `json:"ts,omitzero"`
	// Extra holds unknown wire fields (extra="allow").
	Extra map[string]any `json:"-"`
}

// InputModeChangedFrame announces a change of the worker's input mode.
type InputModeChangedFrame struct {
	Type      string   `json:"type"`
	InputMode string   `json:"input_mode"`
	TS        *float64 `json:"ts,omitzero"`
}

// ---------------------------------------------------------------------------
// Approval gating
// ---------------------------------------------------------------------------

// ApprovalPendingFrame announces a command awaiting operator approval.
type ApprovalPendingFrame struct {
	Type      string  `json:"type"`
	Command   string  `json:"command"`
	RequestID string  `json:"request_id"`
	ExpiresAt float64 `json:"expires_at"`
}

// ApprovalResolvedFrame announces the outcome of an approval request.
type ApprovalResolvedFrame struct {
	Type      string `json:"type"`
	Outcome   string `json:"outcome"`
	RequestID string `json:"request_id"`
}

// ---------------------------------------------------------------------------
// Presence (DeckMux wire format)
// ---------------------------------------------------------------------------

// PresenceUpdateFrame is a DeckMux per-user presence update. The Python model
// is extra="allow" because optional fields (scroll, selection, pin, typing,
// queued_keys) are attached only when relevant — they round-trip via Extra.
type PresenceUpdateFrame struct {
	Type   string  `json:"type"`
	UserID *string `json:"user_id,omitzero"`
	// Extra holds unknown wire fields (extra="allow").
	Extra map[string]any `json:"-"`
}

// PresenceSyncFrame is the full presence roster sent on browser connect.
// The Python model is extra="allow".
type PresenceSyncFrame struct {
	Type    string           `json:"type"`
	Users   []map[string]any `json:"users,omitzero"`
	Config  map[string]any   `json:"config,omitzero"`
	OwnerID *string          `json:"owner_id,omitzero"`
	// Extra holds unknown wire fields (extra="allow").
	Extra map[string]any `json:"-"`
}

// PresenceLeaveFrame announces a user leaving the presence roster.
type PresenceLeaveFrame struct {
	Type   string   `json:"type"`
	UserID string   `json:"user_id"`
	TS     *float64 `json:"ts,omitzero"`
}

// ControlTransferFrame is a DeckMux ownership-transfer notice.
type ControlTransferFrame struct {
	Type       string  `json:"type"`
	FromUserID *string `json:"from_user_id,omitzero"`
	ToUserID   *string `json:"to_user_id,omitzero"`
	Reason     *string `json:"reason,omitzero"`
	QueuedKeys *string `json:"queued_keys,omitzero"`
}
