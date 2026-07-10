//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

// Group is a named set of worker sessions that receive broadcast input
// together. Port of the Python FanOutGroup dataclass (fanout/_models.py).
type Group struct {
	GroupID             string
	Name                string
	WorkerIDs           []string
	CreatedBy           string
	CreatedAt           float64
	Mode                string // "parallel" | "sequential"
	StopOnFirstError    bool
	ErrorPattern        string // "" == Python None
	QuiesceMS           int
	MaxResponseMS       int
	DivergenceThreshold float64
	Grants              []string
}

// SessionResult is the per-session outcome of a fan-out send. Port of
// SessionFanOutResult. OutputDelta is nil when the send failed (the Python
// dataclass uses None), matching the wire shape produced by asdict().
type SessionResult struct {
	WorkerID    string
	OK          bool
	OutputDelta *string
	ElapsedMS   int
	Divergent   bool
}

// Result is the aggregated outcome of a fan-out command sent to a group. Port
// of FanOutResult (the fields the REST + browser-WS responses serialize).
type Result struct {
	GroupID           string
	SendID            string
	Command           string
	SentAt            float64
	Results           []SessionResult
	DivergentSessions []string
	FailedSessions    []string
}

// toMap renders a SessionResult as the asdict() wire shape used by both the
// REST /send response and the browser-WS fanout_result frame.
func (r SessionResult) toMap() map[string]any {
	var delta any
	if r.OutputDelta != nil {
		delta = *r.OutputDelta
	}
	return map[string]any{
		"worker_id":    r.WorkerID,
		"ok":           r.OK,
		"output_delta": delta,
		"elapsed_ms":   r.ElapsedMS,
		"divergent":    r.Divergent,
	}
}

// ResultMaps renders the per-session results as wire maps.
func (r Result) ResultMaps() []map[string]any {
	out := make([]map[string]any, 0, len(r.Results))
	for _, sr := range r.Results {
		out = append(out, sr.toMap())
	}
	return out
}
