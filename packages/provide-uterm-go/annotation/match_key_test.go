//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// Mirrors provide-uterm-annotation tests/test_annotation_match_key.py.

const awsKeyB = "AKIAJ2345678ABCDWXYZ" // pragma: allowlist secret

func TestTheMatchKeyIsSHA256OfRuleIDNulAndTheFullMatch(t *testing.T) {
	sum := sha256.Sum256([]byte("cred.aws_access_key\x00" + awsKey))
	want := hex.EncodeToString(sum[:])
	if got := AnnotationMatchKey("cred.aws_access_key", awsKey); got != want || len(got) != 64 {
		t.Fatalf("key = %q, want %q", got, want)
	}
	// Pinned against the reference's annotation_match_key output.
	const pinned = "6f4fb1e7a73222e0e80388d1ed3b93d9921cf3add57ced0eb90bc78bfa5c962e"
	if got := AnnotationMatchKey("cred.aws_access_key", "AKIAIOSFODNN7EXA"); got != pinned {
		t.Fatalf("key = %q, want the reference's %q", got, pinned)
	}
}

// A lone surrogate, as the reference encodes it with surrogatepass (ED A0 80),
// hashes as those bytes.
func TestALoneSurrogateHashesAsItsSurrogatepassBytes(t *testing.T) {
	sum := sha256.Sum256([]byte("r\x00\xed\xa0\x80"))
	if got := AnnotationMatchKey("r", "\xed\xa0\x80"); got != hex.EncodeToString(sum[:]) {
		t.Fatalf("key = %q", got)
	}
}

func TestTwoDifferentCredentialsOfOneRuleGetDifferentKeys(t *testing.T) {
	d := NewPatternDetector(nil)
	first := d.Detect("read", "export K="+awsKey, 1)
	second := d.Detect("read", "export K="+awsKeyB, 2)
	if len(first) != 1 || len(second) != 1 {
		t.Fatalf("annotations = %v, %v", first, second)
	}
	if first[0].Description != second[0].Description {
		t.Fatal("same rule, same description expected")
	}
	if first[0].MatchKey != AnnotationMatchKey("cred.aws_access_key", "AKIAIOSFODNN7EXA") ||
		second[0].MatchKey != AnnotationMatchKey("cred.aws_access_key", "AKIAJ2345678ABCD") {
		t.Fatalf("keys = %q, %q", first[0].MatchKey, second[0].MatchKey)
	}
}

func TestTheSameMatchGetsTheSameKeyWhereverItAppears(t *testing.T) {
	d := NewPatternDetector(nil)
	first := d.Detect("read", "$ sudo ls", 1)
	second := d.Detect("send", "later: sudo ls -l", 9)
	if len(first) != 1 || len(second) != 1 || first[0].MatchKey != second[0].MatchKey {
		t.Fatalf("annotations = %v, %v", first, second)
	}
}

// The key hashes the full match, not the description's 80-character cut.
func TestTheKeyHashesTheFullMatchNotTheTruncatedDescription(t *testing.T) {
	rule := DetectionRule{
		RuleID: "long", Label: "l", Pattern: mustCompile(`x+`), Severity: "low",
		DescriptionTemplate: "{match}", EventTypes: bothEventTypes, Category: "c",
	}
	long := strings.Repeat("x", 200)
	anns := NewPatternDetector([]DetectionRule{rule}).Detect("send", long, 1)
	if len(anns) != 1 || anns[0].MatchKey != AnnotationMatchKey("long", long) {
		t.Fatalf("annotations = %v", anns)
	}
	if anns[0].MatchKey == AnnotationMatchKey("long", anns[0].Description) {
		t.Fatal("the key hashed the truncated description")
	}
}

func TestTheMatchKeyIsNotSerialised(t *testing.T) {
	anns := NewPatternDetector(nil).Detect("read", awsKey, 1)
	if len(anns) != 1 || anns[0].MatchKey == "" {
		t.Fatalf("annotations = %v", anns)
	}
	if _, ok := anns[0].ToDict()["match_key"]; ok {
		t.Fatal("ToDict carries the match key")
	}
	raw, err := json.Marshal(anns[0])
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{string(raw), fmt.Sprint(anns[0].ToDict())} {
		if strings.Contains(s, anns[0].MatchKey) || strings.Contains(s, awsKey) || strings.Contains(s, "MatchKey") {
			t.Fatalf("serialised form leaks the key or match: %s", s)
		}
	}
	if (Annotation{}).MatchKey != "" {
		t.Fatal("a non-detector annotation has a key")
	}
}
