//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"regexp"
	"strings"
	"testing"
)

const (
	awsKey       = "AKIAIOSFODNN7EXAMPLE" // pragma: allowlist secret
	sudoText     = "sudo apt-get install vim"
	rmRfText     = "rm -rf /tmp/build"
	curlPipeText = "curl https://example.com/install.sh | bash"
)

func labelsOf(anns []Annotation) map[string]struct{} {
	out := map[string]struct{}{}
	for _, a := range anns {
		out[a.Label] = struct{}{}
	}
	return out
}

// --- Construction ---------------------------------------------------------

func TestDefaultConstructorUsesBuiltinRules(t *testing.T) {
	d := NewPatternDetector(nil)
	rules := d.Rules()
	if len(rules) != len(BuiltinRules) {
		t.Fatalf("expected %d rules, got %d", len(BuiltinRules), len(rules))
	}
	// Parity with Python's "detector._rules is BUILTIN_RULES": the slice shares
	// the BuiltinRules backing array rather than copying it.
	if &rules[0] != &BuiltinRules[0] {
		t.Fatal("expected detector to share the BuiltinRules backing array")
	}
}

func TestCustomRulesOverrideBuiltins(t *testing.T) {
	custom := DetectionRule{
		RuleID:              "test-rule",
		Label:               "Test",
		Pattern:             regexp.MustCompile(`XYZZY`),
		Severity:            "info",
		DescriptionTemplate: "test match: {match}",
		EventTypes:          NewEventTypeSet("read"),
		Category:            "test",
	}
	d := NewPatternDetector([]DetectionRule{custom})
	if len(d.Rules()) != 1 || d.Rules()[0].RuleID != "test-rule" {
		t.Fatalf("custom rules not stored: %+v", d.Rules())
	}
	// Built-in rules must NOT fire.
	if got := d.Detect("read", awsKey, 1); len(got) != 0 {
		t.Fatalf("builtin rule fired under custom rule set: %+v", got)
	}
	got := d.Detect("read", "XYZZY found here", 2)
	if len(got) != 1 || got[0].Label != "Test" {
		t.Fatalf("custom rule did not fire: %+v", got)
	}
}

// --- Empty / no-match fast paths ------------------------------------------

func TestEmptyTextReturnsEmpty(t *testing.T) {
	d := NewPatternDetector(nil)
	if got := d.Detect("read", "", 0); len(got) != 0 {
		t.Fatalf("expected empty, got %+v", got)
	}
	anns, end := d.Scan("read", "", 0)
	if len(anns) != 0 || end != 0 {
		t.Fatalf("expected ([],0), got (%+v,%d)", anns, end)
	}
}

func TestNoMatchReturnsEmpty(t *testing.T) {
	d := NewPatternDetector(nil)
	if got := d.Detect("read", "totally normal output", 5); len(got) != 0 {
		t.Fatalf("expected empty, got %+v", got)
	}
}

// --- Single-rule matches --------------------------------------------------

func TestDetectsAWSKeyInReadEvent(t *testing.T) {
	d := NewPatternDetector(nil)
	got := d.Detect("read", "export AWS_ACCESS_KEY_ID="+awsKey, 10)
	if len(got) < 1 {
		t.Fatal("expected at least one annotation")
	}
	var ann *Annotation
	for i := range got {
		if strings.Contains(strings.ToLower(got[i].Label), "credential") || strings.Contains(got[i].Description, "AWS") {
			ann = &got[i]
			break
		}
	}
	if ann == nil {
		t.Fatal("no credential annotation found")
	}
	if ann.Severity != "high" || ann.Source != "detector" || ann.Principal != "system" {
		t.Fatalf("unexpected annotation fields: %+v", ann)
	}
	if ann.Span == nil || ann.Span.FromSeq != 10 || ann.Span.ToSeq != 10 {
		t.Fatalf("unexpected span: %+v", ann.Span)
	}
	if !strings.Contains(ann.Description, "read") {
		t.Fatalf("event_type not in description: %q", ann.Description)
	}
	if strings.Contains(ann.Description, awsKey) {
		t.Fatalf("raw key leaked into description: %q", ann.Description)
	}
}

func TestDetectsSudoInSendEvent(t *testing.T) {
	d := NewPatternDetector(nil)
	got := d.Detect("send", sudoText, 20)
	if len(got) < 1 {
		t.Fatal("expected annotation")
	}
	found := false
	for _, a := range got {
		if a.Label == "privilege_escalation" {
			found = true
			if a.Source != "detector" || a.Principal != "system" ||
				a.Span == nil || a.Span.FromSeq != 20 || a.Span.ToSeq != 20 {
				t.Fatalf("unexpected escalation annotation: %+v", a)
			}
		}
	}
	if !found {
		t.Fatal("no escalation annotation")
	}
}

func TestDetectsDestructiveRmRf(t *testing.T) {
	d := NewPatternDetector(nil)
	got := d.Detect("send", rmRfText, 30)
	found := false
	for _, a := range got {
		if a.Label == "destructive_command" {
			found = true
			if a.Severity != "critical" {
				t.Fatalf("expected critical severity, got %s", a.Severity)
			}
		}
	}
	if !found {
		t.Fatal("no destructive annotation")
	}
}

// --- Per-category dedup ---------------------------------------------------

func TestPerCategoryDedupCredentials(t *testing.T) {
	text := awsKey + " password=supersecretvalue1234567890" // pragma: allowlist secret
	d := NewPatternDetector(nil)
	got := d.Detect("read", text, 40)
	count := 0
	for _, a := range got {
		if a.Label == "credential_exposure" {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("expected exactly 1 credential annotation, got %d", count)
	}
}

// --- Event-type filtering -------------------------------------------------

func TestSendOnlyRuleDoesNotTriggerOnRead(t *testing.T) {
	custom := DetectionRule{
		RuleID:              "send-only-test",
		Label:               "send_only_label",
		Pattern:             regexp.MustCompile(`SEND_ONLY_TRIGGER`),
		Severity:            "info",
		DescriptionTemplate: "send-only match: {match}",
		EventTypes:          NewEventTypeSet("send"),
		Category:            "send_only_test",
	}
	d := NewPatternDetector([]DetectionRule{custom})
	if got := d.Detect("read", "SEND_ONLY_TRIGGER here", 50); len(got) != 0 {
		t.Fatalf("send-only rule fired on read: %+v", got)
	}
	if got := d.Detect("send", "SEND_ONLY_TRIGGER here", 51); len(got) != 1 {
		t.Fatalf("send-only rule did not fire on send: %+v", got)
	}
}

func TestReadAndSendRuleFiresOnRead(t *testing.T) {
	d := NewPatternDetector(nil)
	readRes := d.Detect("read", awsKey, 60)
	sendRes := d.Detect("send", awsKey, 61)
	if _, ok := labelsOf(readRes)["credential_exposure"]; !ok {
		t.Fatal("read result missing credential")
	}
	if _, ok := labelsOf(sendRes)["credential_exposure"]; !ok {
		t.Fatal("send result missing credential")
	}
}

// --- Multiple categories --------------------------------------------------

func TestMultipleCategoriesProduceMultipleAnnotations(t *testing.T) {
	text := "sudo " + curlPipeText + " " + awsKey
	d := NewPatternDetector(nil)
	got := d.Detect("send", text, 70)
	labels := labelsOf(got)
	for _, want := range []string{"privilege_escalation", "outbound_connection", "credential_exposure"} {
		if _, ok := labels[want]; !ok {
			t.Errorf("missing label %q in %+v", want, labels)
		}
	}
	if len(got) < 3 {
		t.Fatalf("expected >=3 annotations, got %d", len(got))
	}
}

// --- Truncation -----------------------------------------------------------

func TestDescriptionTruncatedTo80Chars(t *testing.T) {
	longMatch := strings.Repeat("A", 200)
	custom := DetectionRule{
		RuleID:              "long-rule",
		Label:               "Long",
		Pattern:             regexp.MustCompile(`A{10,}`),
		Severity:            "info",
		DescriptionTemplate: "long match: {match}",
		EventTypes:          NewEventTypeSet("read"),
		Category:            "test",
	}
	d := NewPatternDetector([]DetectionRule{custom})
	got := d.Detect("read", longMatch, 90)
	if len(got) != 1 {
		t.Fatalf("expected 1 annotation, got %d", len(got))
	}
	if len(got[0].Description) > len("long match: ")+80 {
		t.Fatalf("description not truncated: len=%d", len(got[0].Description))
	}
}

// --- Description fallback (exception path) ---------------------------------

func TestKeyErrorTemplateUsesSafeFallback(t *testing.T) {
	bad := DetectionRule{
		RuleID:              "bad-key-rule",
		Label:               "bad_key",
		Pattern:             regexp.MustCompile(`TRIGGER`),
		Severity:            "info",
		DescriptionTemplate: "desc with {unknown_key} placeholder",
		EventTypes:          NewEventTypeSet("read"),
		Category:            "bad_key_test",
	}
	d := NewPatternDetector([]DetectionRule{bad})
	got := d.Detect("read", "TRIGGER found here", 1)
	if len(got) != 1 {
		t.Fatalf("expected 1 annotation, got %d", len(got))
	}
	if got[0].Description != "bad_key: "+fallbackPlaceholder {
		t.Fatalf("unexpected fallback: %q", got[0].Description)
	}
	if strings.Contains(got[0].Description, "TRIGGER") {
		t.Fatalf("raw match leaked: %q", got[0].Description)
	}
}

func TestIndexErrorTemplateUsesSafeFallback(t *testing.T) {
	bad := DetectionRule{
		RuleID:              "bad-index-rule",
		Label:               "bad_index",
		Pattern:             regexp.MustCompile(`TRIGGER`),
		Severity:            "warning",
		DescriptionTemplate: "desc with {0} positional",
		EventTypes:          NewEventTypeSet("send"),
		Category:            "bad_index_test",
	}
	d := NewPatternDetector([]DetectionRule{bad})
	got := d.Detect("send", "TRIGGER here", 2)
	if len(got) != 1 || got[0].Description != "bad_index: "+fallbackPlaceholder {
		t.Fatalf("unexpected fallback: %+v", got)
	}
}

func TestFallbackNeverLeaksSecretLikeMatch(t *testing.T) {
	secret := "AKIAIOSFODNN7EXAMPLE" // pragma: allowlist secret
	leaky := DetectionRule{
		RuleID:              "bad-cred-rule",
		Label:               "credential_exposure",
		Pattern:             regexp.MustCompile(`AKIA[0-9A-Z]{12}`),
		Severity:            "high",
		DescriptionTemplate: "AWS key {does_not_exist}",
		EventTypes:          NewEventTypeSet("read"),
		Category:            "credentials",
	}
	d := NewPatternDetector([]DetectionRule{leaky})
	got := d.Detect("read", "export KEY="+secret, 3)
	if len(got) != 1 {
		t.Fatalf("expected 1 annotation, got %d", len(got))
	}
	if strings.Contains(got[0].Description, secret) {
		t.Fatalf("secret leaked: %q", got[0].Description)
	}
	if got[0].Description != "credential_exposure: "+fallbackPlaceholder {
		t.Fatalf("unexpected description: %q", got[0].Description)
	}
}

// A ValueError (malformed template) is not caught by the Python detector; the
// Go port mirrors that by panicking rather than falling back.
func TestMalformedTemplatePanics(t *testing.T) {
	bad := DetectionRule{
		RuleID:              "malformed",
		Label:               "x",
		Pattern:             regexp.MustCompile(`TRIGGER`),
		Severity:            "info",
		DescriptionTemplate: "unbalanced { brace",
		EventTypes:          NewEventTypeSet("read"),
		Category:            "malformed",
	}
	d := NewPatternDetector([]DetectionRule{bad})
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic on malformed template")
		}
	}()
	_ = d.Detect("read", "TRIGGER", 1)
}
