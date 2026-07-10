//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"reflect"
	"regexp"
	"testing"
)

// --- Annotation defaults --------------------------------------------------

func TestAnnotationSpanDefaultsToNil(t *testing.T) {
	a := Annotation{
		Label:       "cred-exposure",
		Description: "Password visible in output",
		Severity:    "high",
		Source:      "detector",
		Principal:   "user1",
	}
	if a.Span != nil {
		t.Fatalf("expected nil span, got %+v", a.Span)
	}
}

func TestAnnotationRequiredFieldsStored(t *testing.T) {
	a := Annotation{
		Label:       "priv-esc",
		Description: "sudo used",
		Severity:    "critical",
		Source:      "rule-engine",
		Principal:   "admin",
	}
	if a.Label != "priv-esc" || a.Description != "sudo used" || a.Severity != "critical" ||
		a.Source != "rule-engine" || a.Principal != "admin" {
		t.Fatalf("field mismatch: %+v", a)
	}
}

// --- Annotation with span -------------------------------------------------

func TestAnnotationSpanStored(t *testing.T) {
	span := &AnnotationSpan{FromSeq: 10, ToSeq: 20}
	a := Annotation{
		Label:       "data-exfil",
		Description: "Large data transfer",
		Severity:    "medium",
		Source:      "heuristic",
		Principal:   "user2",
		Span:        span,
	}
	if a.Span == nil || a.Span.FromSeq != 10 || a.Span.ToSeq != 20 {
		t.Fatalf("span not stored: %+v", a.Span)
	}
}

func TestAnnotationSpanFields(t *testing.T) {
	span := AnnotationSpan{FromSeq: 0, ToSeq: 999}
	if span.FromSeq != 0 || span.ToSeq != 999 {
		t.Fatalf("unexpected span fields: %+v", span)
	}
}

// --- Annotation.ToDict ----------------------------------------------------

func TestToDictWithoutSpan(t *testing.T) {
	a := Annotation{
		Label:       "cred-exposure",
		Description: "Password in output",
		Severity:    "high",
		Source:      "detector",
		Principal:   "svc-account",
	}
	got := a.ToDict()
	want := map[string]any{
		"label":       "cred-exposure",
		"description": "Password in output",
		"severity":    "high",
		"source":      "detector",
		"principal":   "svc-account",
		"span":        nil,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("ToDict mismatch\n got: %#v\nwant: %#v", got, want)
	}
}

func TestToDictWithSpan(t *testing.T) {
	a := Annotation{
		Label:       "priv-esc",
		Description: "sudo invoked",
		Severity:    "critical",
		Source:      "rule-engine",
		Principal:   "deploy-bot",
		Span:        &AnnotationSpan{FromSeq: 5, ToSeq: 15},
	}
	got := a.ToDict()
	wantSpan := map[string]any{"from_seq": 5, "to_seq": 15}
	if !reflect.DeepEqual(got["span"], wantSpan) {
		t.Fatalf("span mismatch: %#v", got["span"])
	}
}

func TestToDictSpanIsNestedMap(t *testing.T) {
	a := Annotation{Span: &AnnotationSpan{FromSeq: 1, ToSeq: 3}}
	got := a.ToDict()
	if _, ok := got["span"].(map[string]any); !ok {
		t.Fatalf("span should be a nested map, got %T", got["span"])
	}
}

// --- DetectionRule --------------------------------------------------------

func TestDetectionRuleFieldAccess(t *testing.T) {
	pattern := regexp.MustCompile(`(?i)password\s*=\s*\S+`)
	rule := DetectionRule{
		RuleID:              "CRED-001",
		Label:               "credential-exposure",
		Pattern:             pattern,
		Severity:            "high",
		DescriptionTemplate: "Credential exposed: {match}",
		EventTypes:          NewEventTypeSet("output", "screen"),
		Category:            "credential",
	}
	if rule.RuleID != "CRED-001" || rule.Label != "credential-exposure" || rule.Pattern != pattern ||
		rule.Severity != "high" || rule.DescriptionTemplate != "Credential exposed: {match}" ||
		rule.Category != "credential" {
		t.Fatalf("field mismatch: %+v", rule)
	}
	if !reflect.DeepEqual(rule.EventTypes, NewEventTypeSet("output", "screen")) {
		t.Fatalf("event types mismatch: %v", rule.EventTypes)
	}
}

func TestDetectionRulePatternMatches(t *testing.T) {
	pattern := regexp.MustCompile(`sudo\s+\w+`)
	rule := DetectionRule{Pattern: pattern}
	if rule.Pattern.FindStringIndex("sudo rm -rf /") == nil {
		t.Fatal("expected match for 'sudo rm -rf /'")
	}
	if rule.Pattern.FindStringIndex("ls -la") != nil {
		t.Fatal("expected no match for 'ls -la'")
	}
}

func TestDetectionRuleAppliesTo(t *testing.T) {
	rule := DetectionRule{EventTypes: NewEventTypeSet("output", "keystroke")}
	if !rule.AppliesTo("output") {
		t.Fatal("expected AppliesTo(output) true")
	}
	if rule.AppliesTo("send") {
		t.Fatal("expected AppliesTo(send) false")
	}
}
