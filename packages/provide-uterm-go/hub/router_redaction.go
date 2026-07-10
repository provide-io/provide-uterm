//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

// Frame-field redaction helpers for the message router. Port of
// provide.uterm.server.bridge.hub.router_redaction. These are pure, stateless
// helpers: they take a [StreamRedactor] and return redacted COPIES of wire
// frames / nested payloads; they never mutate their inputs.

// redactMaxDepth is the defensive recursion bound for redactValue. Wire frames
// are JSON-decoded (acyclic) and shallow in practice; a value below the cap is
// returned as-is rather than redacted — failing closed on depth (returning raw)
// is the conservative choice on a hot broadcast path. Port of _REDACT_MAX_DEPTH.
const redactMaxDepth = 32

// RedactFrameFields is the concrete [Redactor] that wires the real
// [StreamRedactor] into the hub's output-policy seam. It builds a redactor from
// rules and returns a role-redacted copy of msg. Assign it to
// TermHubConfig.Redactor to activate output redaction when a gate yields rules.
//
// This mirrors the Python broadcast path, which calls
// _redact_frame_fields(msg, StreamRedactor(rules)) directly (Python has no
// pluggable-redactor seam — StreamRedactor is hardcoded there).
func RedactFrameFields(msg map[string]any, rules []RedactionRule) map[string]any {
	return redactFrameFields(msg, NewStreamRedactor(rules))
}

// redactValue recursively redacts string values inside nested map/slice
// structures. Strings are redacted; maps and slices are walked (values/elements
// only, not map keys); all other scalars are returned unchanged. Recursion is
// capped at redactMaxDepth. The input is never mutated. Port of _redact_value.
func redactValue(value any, r *StreamRedactor, depth int) any {
	if s, ok := value.(string); ok {
		return r.Redact(s)
	}
	if depth >= redactMaxDepth {
		return value
	}
	switch v := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(v))
		for k, vv := range v {
			out[k] = redactValue(vv, r, depth+1)
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, vv := range v {
			out[i] = redactValue(vv, r, depth+1)
		}
		return out
	default:
		return value
	}
}

// redactFrameFields returns a copy of msg with its terminal-content string fields
// redacted, for term / snapshot / analysis frames; other frame types are
// returned unchanged (same reference). Port of _redact_frame_fields.
func redactFrameFields(msg map[string]any, r *StreamRedactor) map[string]any {
	switch str(msg["type"]) {
	case "term":
		out := copyMap(msg)
		out["data"] = r.Redact(str(msg["data"]))
		return out
	case "snapshot":
		out := copyMap(msg)
		out["screen"] = r.Redact(str(msg["screen"]))
		if rawTail, ok := msg["raw_tail"].(string); ok {
			out["raw_tail"] = r.Redact(rawTail)
		}
		// prompt_detected can carry the matched prompt text (which may include
		// secrets); redact its nested string values. Absent/scalar stays as-is.
		if pd, ok := out["prompt_detected"]; ok {
			out["prompt_detected"] = redactValue(pd, r, 0)
		}
		return out
	case "analysis":
		out := copyMap(msg)
		out["formatted"] = r.Redact(str(msg["formatted"]))
		switch raw := msg["raw"].(type) {
		case string:
			out["raw"] = r.Redact(raw)
		case map[string]any:
			out["raw"] = redactValue(raw, r, 0)
		case []any:
			out["raw"] = redactValue(raw, r, 0)
		}
		return out
	default:
		return msg
	}
}

// copyMap returns a shallow copy of m (a new top-level map sharing the original
// values). Redaction helpers build new containers so the stored frame is never
// mutated.
func copyMap(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
