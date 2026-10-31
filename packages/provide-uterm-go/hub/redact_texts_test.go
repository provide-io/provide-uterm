//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"errors"
	"reflect"
	"testing"
)

// RedactTextsForRecipient applies the live screen's output policy to text that
// is not a frame (an ended session's screen and scrollback).

func TestTextsAreRedactedByTheOutputPolicy(t *testing.T) {
	h, _ := newTestHub(t, func(c *TermHubConfig) {
		c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "secret", Replacement: "[REDACTED]"}}}
		c.Redactor = RedactFrameFields
	})
	got, err := h.RedactTextsForRecipient(bg(), "w1", []string{"a secret", "none"}, newBrowserWS("r"))
	if err != nil || !reflect.DeepEqual(got, []string{"a [REDACTED]", "none"}) {
		t.Fatalf("got %q, %v", got, err)
	}
}

func TestTextsPassUnchangedWithoutAPolicyToApply(t *testing.T) {
	texts := []string{"a secret"}
	for name, opt := range map[string]func(*TermHubConfig){
		"no gate":  func(*TermHubConfig) {},
		"no rules": func(c *TermHubConfig) { c.OutputPolicyGate = staticOutputGate{}; c.Redactor = RedactFrameFields },
		"no redactor": func(c *TermHubConfig) {
			c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "secret"}}}
		},
	} {
		h, _ := newTestHub(t, opt)
		got, err := h.RedactTextsForRecipient(bg(), "w1", texts, newBrowserWS("r"))
		if err != nil || !reflect.DeepEqual(got, texts) {
			t.Errorf("%s: got %q, %v", name, got, err)
		}
	}
}

// A policy that cannot answer is an error, never the unredacted text.
func TestTextsAreWithheldWhenThePolicyCannotAnswer(t *testing.T) {
	for name, opt := range map[string]func(*TermHubConfig){
		"gate": func(c *TermHubConfig) { c.OutputPolicyGate = staticOutputGate{err: errors.New("down")} },
		"context": func(c *TermHubConfig) {
			c.OutputPolicyGate = staticOutputGate{rules: []RedactionRule{{Pattern: "x"}}}
			c.IdentityProvider = errIdentityProvider{}
		},
	} {
		h, _ := newTestHub(t, opt)
		if got, err := h.RedactTextsForRecipient(bg(), "w1", []string{"a secret"}, newBrowserWS("r")); err == nil || got != nil {
			t.Errorf("%s: got %q, %v", name, got, err)
		}
	}
}
