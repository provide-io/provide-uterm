//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package annotation is a behavior-faithful Go port of the Python module
// provide.uterm.annotation: data models, built-in detection rules, the
// hot-path PatternDetector, and the stateful StreamingDetector that bridges
// pattern matches split across consecutive chunks.
//
// The annotation layer marks and detects interesting moments in terminal
// session recordings (credential exposure, privilege escalation, destructive
// commands, outbound connections, and session lifecycle events).
//
// Python regexes are compiled under Go's RE2 engine. The built-in rules are
// all RE2-compatible; custom rules that use constructs RE2 cannot express
// (lookaround, backreferences) surface a *RE2TranslateError from
// CompilePattern rather than being silently dropped.
package annotation

import "regexp"

// AnnotationSpan is a contiguous range of recording event sequence numbers.
type AnnotationSpan struct {
	FromSeq int
	ToSeq   int
}

// Annotation is a single annotation marking an interesting moment (or range)
// in a session recording. A nil Span mirrors the Python default of None.
type Annotation struct {
	Label       string
	Description string
	Severity    string
	Source      string
	Principal   string
	Span        *AnnotationSpan
}

// ToDict serialises to a plain map, with "span" as a nested map when present
// and nil otherwise. The key set and shape match the Python Annotation.to_dict
// output exactly (label, description, severity, source, principal, span).
func (a Annotation) ToDict() map[string]any {
	result := map[string]any{
		"label":       a.Label,
		"description": a.Description,
		"severity":    a.Severity,
		"source":      a.Source,
		"principal":   a.Principal,
		"span":        nil,
	}
	if a.Span != nil {
		result["span"] = map[string]any{
			"from_seq": a.Span.FromSeq,
			"to_seq":   a.Span.ToSeq,
		}
	}
	return result
}

// DetectionRule is a compiled regex rule used to detect annotation-worthy
// events in terminal output. EventTypes is the set of event types the rule
// applies to (mirroring the Python frozenset).
type DetectionRule struct {
	RuleID              string
	Label               string
	Pattern             *regexp.Regexp
	Severity            string
	DescriptionTemplate string
	EventTypes          EventTypeSet
	Category            string
}

// AppliesTo reports whether the rule applies to eventType. Mirrors the Python
// membership test "event_type in rule.event_types".
func (r DetectionRule) AppliesTo(eventType string) bool {
	_, ok := r.EventTypes[eventType]
	return ok
}

// EventTypeSet is the Go analogue of a Python frozenset[str] of event types.
type EventTypeSet map[string]struct{}

// NewEventTypeSet builds an EventTypeSet from the given event types.
func NewEventTypeSet(eventTypes ...string) EventTypeSet {
	s := make(EventTypeSet, len(eventTypes))
	for _, e := range eventTypes {
		s[e] = struct{}{}
	}
	return s
}
