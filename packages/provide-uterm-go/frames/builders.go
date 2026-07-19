//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

import "time"

// Builder helpers mirroring the Python builders in
// provide-uterm-server/.../bridge/frames.py (and the core
// provide.uterm.frames.make_snapshot_frame).
//
// Timestamp convention: the Python builders take ``ts: float | None = None``
// and stamp ``time.time()`` when ts is None. Go has no None, so every
// builder takes an explicit ts argument and treats ts <= 0 as "stamp
// time.Now()". A caller that genuinely needs ts == 0.0 (or a negative ts)
// on the wire can set the frame's TS field directly after construction.

// Ptr returns a pointer to v — a convenience for populating optional
// (pointer-typed) frame fields.
func Ptr[T any](v T) *T { return &v }

// nowTS returns the current Unix time in seconds, matching Python's
// time.time().
func nowTS() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// tsOrNow returns ts when positive, otherwise the current Unix time.
func tsOrNow(ts float64) float64 {
	if ts > 0 {
		return ts
	}
	return nowTS()
}

// MakeErrorFrame mirrors make_error_frame(message).
func MakeErrorFrame(message string) ErrorFrame {
	return ErrorFrame{Type: TypeError, Message: message}
}

// MakePongFrame mirrors make_pong_frame(ts=...); ts <= 0 means "now".
func MakePongFrame(ts float64) PongFrame {
	return PongFrame{Type: TypePong, TS: Ptr(tsOrNow(ts))}
}

// MakeHeartbeatAckFrame mirrors make_heartbeat_ack_frame(lease_expires_at,
// ts=...); ts <= 0 means "now".
func MakeHeartbeatAckFrame(leaseExpiresAt, ts float64) HeartbeatAckFrame {
	return HeartbeatAckFrame{
		Type:           TypeHeartbeatAck,
		LeaseExpiresAt: leaseExpiresAt,
		TS:             Ptr(tsOrNow(ts)),
	}
}

// MakeWorkerConnectedFrame mirrors make_worker_connected_frame(worker_id,
// ts=...); ts <= 0 means "now".
func MakeWorkerConnectedFrame(workerID string, ts float64) WorkerConnectedFrame {
	return WorkerConnectedFrame{Type: TypeWorkerConnected, WorkerID: workerID, TS: Ptr(tsOrNow(ts))}
}

// MakeWorkerDisconnectedFrame mirrors make_worker_disconnected_frame(
// worker_id, ts=...); ts <= 0 means "now".
func MakeWorkerDisconnectedFrame(workerID string, ts float64) WorkerDisconnectedFrame {
	return WorkerDisconnectedFrame{Type: TypeWorkerDisconnected, WorkerID: workerID, TS: Ptr(tsOrNow(ts))}
}

// MakeTermFrame mirrors make_term_frame(data, ts=...); ts <= 0 means "now".
func MakeTermFrame(data string, ts float64) TermFrame {
	return TermFrame{Type: TypeTerm, Data: data, TS: Ptr(tsOrNow(ts))}
}

// SnapshotParams carries the keyword arguments of make_snapshot_frame.
// PromptDetected and RawTail are optional (nil allowed); TS <= 0 means
// "now" (the Python builder's ts=None default).
type SnapshotParams struct {
	Screen           string
	Cursor           map[string]int
	Cols             int
	Rows             int
	ScreenHash       string
	CursorAtEnd      bool
	HasTrailingSpace bool
	PromptDetected   map[string]any
	TS               float64
	RawTail          *string
}

// MakeSnapshotFrame mirrors the core make_snapshot_frame builder. Note the
// package-level deviation: the Python builder dumps with exclude_none=False
// (emitting "prompt_detected": null / "raw_tail": null when unset), while
// the Go frame omits those keys.
func MakeSnapshotFrame(p SnapshotParams) SnapshotFrame {
	return SnapshotFrame{
		Type:             TypeSnapshot,
		Screen:           p.Screen,
		Cursor:           p.Cursor,
		Cols:             Ptr(p.Cols),
		Rows:             Ptr(p.Rows),
		ScreenHash:       Ptr(p.ScreenHash),
		CursorAtEnd:      Ptr(p.CursorAtEnd),
		HasTrailingSpace: Ptr(p.HasTrailingSpace),
		PromptDetected:   p.PromptDetected,
		RawTail:          p.RawTail,
		TS:               Ptr(tsOrNow(p.TS)),
	}
}

// MakeAnalysisFrame mirrors make_analysis_frame(formatted=..., raw=...,
// ts=...); ts <= 0 means "now". Deviation: the Python builder emits
// "raw": null when raw is None (exclude_none=False); the Go frame omits the
// key when raw is nil.
func MakeAnalysisFrame(formatted string, raw any, ts float64) AnalysisFrame {
	return AnalysisFrame{Type: TypeAnalysis, Formatted: formatted, Raw: raw, TS: Ptr(tsOrNow(ts))}
}

// MakeHijackStateFrame mirrors make_hijack_state_frame(hijacked=...,
// owner=..., lease_expires_at=..., input_mode=...). Deviation: the Python
// builder emits "owner": null / "lease_expires_at": null when unset
// (exclude_none=False); the Go frame omits the keys when nil.
func MakeHijackStateFrame(hijacked bool, owner *string, leaseExpiresAt *float64, inputMode string) HijackStateFrame {
	return HijackStateFrame{
		Type:           TypeHijackState,
		Hijacked:       hijacked,
		Owner:          owner,
		LeaseExpiresAt: leaseExpiresAt,
		InputMode:      Ptr(inputMode),
	}
}

// MakeHelloFrame mirrors the type stamp of make_hello_frame(); callers set
// capability fields. Prefer MakeHelloFrameWithDefaults for server hellos.
func MakeHelloFrame() HelloFrame {
	return HelloFrame{Type: TypeHello}
}

// MakeHelloFrameWithDefaults applies Go defaults from spec/behavior.json:
// mcp_supported=true, vnc_supported=true.
func MakeHelloFrameWithDefaults() HelloFrame {
	mcp, vnc := true, true
	h := MakeHelloFrame()
	h.McpSupported = &mcp
	h.VncSupported = &vnc
	return h
}

// NewIdentityFrame builds an IdentityFrame for subject with the Pydantic
// field defaults applied: version=1, fingerprint="", transport="ssh".
func NewIdentityFrame(subject string) IdentityFrame {
	return IdentityFrame{
		Type:        TypeIdentity,
		Version:     IdentityDefaultVersion,
		Subject:     subject,
		Fingerprint: IdentityDefaultFingerprint,
		Transport:   IdentityDefaultTransport,
	}
}

// CoerceWorkerStatusFrame mirrors coerce_worker_status_frame(payload): it
// copies the payload into a StatusFrame, defaulting "type" to "status" and
// "ts" to time.Now() when absent. A string "type" and a numeric "ts"
// (float64 or int) populate the modelled fields; every other key — and a
// non-numeric "ts" — lands in Extra.
func CoerceWorkerStatusFrame(payload map[string]any) StatusFrame {
	f := StatusFrame{Type: TypeStatus}
	hasTS := false
	for k, v := range payload {
		switch k {
		case "type":
			if s, ok := v.(string); ok {
				f.Type = s
				continue
			}
		case "ts":
			hasTS = true
			switch n := v.(type) {
			case float64:
				f.TS = Ptr(n)
				continue
			case int:
				f.TS = Ptr(float64(n))
				continue
			}
		}
		if f.Extra == nil {
			f.Extra = make(map[string]any)
		}
		f.Extra[k] = v
	}
	if !hasTS {
		f.TS = Ptr(nowTS())
	}
	return f
}
