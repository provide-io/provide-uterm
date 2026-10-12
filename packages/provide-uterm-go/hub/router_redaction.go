//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "github.com/provide-io/provide-uterm/packages/provide-uterm-go/redaction"

// Frame-field redaction helpers for the message router. Port of
// provide.uterm.server.bridge.hub.router_redaction. These are pure, stateless
// helpers: they take a [StreamRedactor] and return redacted COPIES of wire
// frames / nested payloads; they never mutate their inputs.

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

// redactValue returns a redacted copy of a nested map/slice structure via the
// shared walker, redaction.RedactStructure: strings are redacted at any depth,
// containers are rebuilt (map keys kept), other scalars pass through, and the
// input is never mutated. A container redaction.RedactMaxDepth levels down is
// replaced whole by redaction.DepthLimitMarker -- it is never returned
// unwalked, which would ship any secret nested below the cap. With keyAware,
// values under credential field names are masked too (annotation events);
// broadcast frames keep key-aware off. Port of _redact_value (and, with
// keyAware, of the annotation branch of _redact_event_payload).
func redactValue(value any, r *StreamRedactor, keyAware bool) any {
	return redaction.RedactStructure(value, r.Redact, redaction.StructureOptions{KeyAware: keyAware})
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
			out["prompt_detected"] = redactValue(pd, r, false)
		}
		return out
	case "analysis":
		out := copyMap(msg)
		out["formatted"] = r.Redact(str(msg["formatted"]))
		switch raw := msg["raw"].(type) {
		case string:
			out["raw"] = r.Redact(raw)
		case map[string]any:
			out["raw"] = redactValue(raw, r, false)
		case []any:
			out["raw"] = redactValue(raw, r, false)
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
