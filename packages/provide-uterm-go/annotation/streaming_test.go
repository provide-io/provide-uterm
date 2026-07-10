//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import "testing"

// An AWS access key: literal "AKIA" + 12 uppercase/digits = 16 chars total.
const streamKey = "AKIA0123456789AB"

func hasLabel(anns []Annotation, label string) bool {
	for _, a := range anns {
		if a.Label == label {
			return true
		}
	}
	return false
}

func TestBareDetectorMissesSplitPattern(t *testing.T) {
	d := NewPatternDetector(nil)
	if got := d.Detect("send", streamKey[:8], 1); len(got) != 0 {
		t.Fatalf("first half should not match: %+v", got)
	}
	if got := d.Detect("send", streamKey[8:], 2); len(got) != 0 {
		t.Fatalf("second half should not match: %+v", got)
	}
}

func TestStreamingDetectorCatchesSplitPattern(t *testing.T) {
	sd := NewStreamingDetector(NewPatternDetector(nil), 0)
	if got := sd.Detect("send", streamKey[:8], 1); len(got) != 0 {
		t.Fatalf("chunk 1 should be incomplete: %+v", got)
	}
	out := sd.Detect("send", streamKey[8:], 2)
	if len(out) != 1 || out[0].Label != "credential_exposure" {
		t.Fatalf("expected 1 credential annotation, got %+v", out)
	}
	if out[0].Span == nil || out[0].Span.FromSeq != 2 {
		t.Fatalf("match should be owned by completing chunk: %+v", out[0].Span)
	}
}

func TestWholePatternInOneChunkStillMatches(t *testing.T) {
	sd := NewStreamingDetector(NewPatternDetector(nil), 0)
	out := sd.Detect("send", "export KEY="+streamKey, 5)
	if len(out) != 1 || out[0].Span == nil || out[0].Span.FromSeq != 5 {
		t.Fatalf("unexpected: %+v", out)
	}
}

func TestNoReemitOnFollowingChunk(t *testing.T) {
	sd := NewStreamingDetector(NewPatternDetector(nil), 0)
	sd.Detect("send", streamKey[:8], 1)
	first := sd.Detect("send", streamKey[8:], 2)
	if len(first) != 1 {
		t.Fatalf("expected 1 match, got %+v", first)
	}
	if got := sd.Detect("send", "nothing here", 3); len(got) != 0 {
		t.Fatalf("should not re-emit: %+v", got)
	}
}

func TestSecondSplitPatternAfterHitStillBridges(t *testing.T) {
	sd := NewStreamingDetector(NewPatternDetector(nil), 0)
	first := sd.Detect("send", "sudo AKIA0123", 1)
	if !hasLabel(first, "privilege_escalation") {
		t.Fatalf("expected escalation in chunk 1: %+v", first)
	}
	if hasLabel(first, "credential_exposure") {
		t.Fatalf("key should not be complete yet: %+v", first)
	}
	second := sd.Detect("send", "456789AB ok", 2)
	if !hasLabel(second, "credential_exposure") {
		t.Fatalf("expected credential in chunk 2: %+v", second)
	}
}

func TestEmptyTextReturnsEmptyAndKeepsCarry(t *testing.T) {
	sd := NewStreamingDetector(NewPatternDetector(nil), 0)
	sd.Detect("send", streamKey[:8], 1)
	if got := sd.Detect("send", "", 2); len(got) != 0 {
		t.Fatalf("empty text should short-circuit: %+v", got)
	}
	if got := sd.Detect("send", streamKey[8:], 3); len(got) != 1 {
		t.Fatalf("carry should survive empty call: %+v", got)
	}
}

func TestResetDropsCarry(t *testing.T) {
	sd := NewStreamingDetector(NewPatternDetector(nil), 0)
	sd.Detect("send", streamKey[:8], 1)
	sd.Reset()
	if got := sd.Detect("send", streamKey[8:], 2); len(got) != 0 {
		t.Fatalf("reset should forget carry: %+v", got)
	}
}

func TestCarryIsBounded(t *testing.T) {
	sd := NewStreamingDetector(NewPatternDetector(nil), 4)
	if got := sd.Detect("send", streamKey[:8], 1); len(got) != 0 {
		t.Fatalf("chunk 1: %+v", got)
	}
	if got := sd.Detect("send", streamKey[8:], 2); len(got) != 0 {
		t.Fatalf("bounded carry should not bridge: %+v", got)
	}
}
