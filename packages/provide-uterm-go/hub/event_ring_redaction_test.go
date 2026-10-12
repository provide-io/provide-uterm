//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/redaction"
)

// A token the default ruleset recognises (GitHub PAT shape).
const ringSecret = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

func TestRedactedEventTypesPortThePythonSet(t *testing.T) {
	for _, typ := range []string{"term", "snapshot", "analysis", "annotation"} {
		mustTrue(t, redactedEventTypes[typ], typ+" redacted at write time")
	}
	mustEqual(t, len(redactedEventTypes), 4, "exactly the Python _REDACTED_EVENT_TYPES")
	mustFalse(t, contentEventTypes["annotation"], "the broadcast gate set is a different, frame-level set")
}

func TestAppendEventRedactsAnnotationKeyAware(t *testing.T) {
	h, _ := newTestHub(t, nil)
	_, err := h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	mustEqual(t, err, nil, "register")
	in := map[string]any{
		"description": "rotated " + ringSecret,
		"meta":        map[string]any{"accessToken": "opaque", "token": nil},
	}
	evt, err := h.Router.AppendEvent(bg(), "w1", "annotation", in)
	mustEqual(t, err, nil, "append")
	data := evt["data"].(map[string]any)
	mustEqual(t, data["description"].(string), "rotated [GITHUB_TOKEN_REDACTED]", "text redacted")
	meta := data["meta"].(map[string]any)
	mustEqual(t, meta["accessToken"].(string), redaction.SensitiveKeyMarker, "credential field masked")
	mustTrue(t, meta["token"] == nil, "null credential stays null")
	mustEqual(t, in["description"].(string), "rotated "+ringSecret, "input not mutated")
	stored := h.registry.Get("w1").Events
	mustEqual(t, stored[len(stored)-1]["data"].(map[string]any)["description"].(string),
		"rotated [GITHUB_TOKEN_REDACTED]", "ring copy redacted")
}

func TestAppendEventRedactsTermButKeepsNonStringData(t *testing.T) {
	h, _ := newTestHub(t, nil)
	_, _ = h.Conn.RegisterWorker(bg(), "w1", &fakeWorkerWS{})
	evt, _ := h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": "x " + ringSecret})
	mustEqual(t, evt["data"].(map[string]any)["data"].(string), "x [GITHUB_TOKEN_REDACTED]", "term redacted")
	_, hasType := evt["data"].(map[string]any)["type"]
	mustFalse(t, hasType, "synthetic type key stripped")
	evt, _ = h.Router.AppendEvent(bg(), "w1", "term", map[string]any{"data": 42})
	mustEqual(t, evt["data"].(map[string]any)["data"].(int), 42, "non-string term data kept")
	evt, _ = h.Router.AppendEvent(bg(), "w1", "snapshot", map[string]any{"screen": ringSecret})
	mustEqual(t, evt["data"].(map[string]any)["screen"].(string), "[GITHUB_TOKEN_REDACTED]", "snapshot redacted")
	evt, _ = h.Router.AppendEvent(bg(), "w1", "input_send", map[string]any{"keys": ringSecret})
	mustEqual(t, evt["data"].(map[string]any)["keys"].(string), ringSecret, "control events untouched")
}
