//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/annotation"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/screen"
)

// --- annotation -------------------------------------------------------------

// The corpus's annotated script, recorded by a runtime given the reference's
// PatternDetector: a send split mid-word, styled streamed output, a match
// split across frames, an empty frame, a screen matching several categories,
// input masked at a prompt yet still annotated, a styled screen, an escape
// sequence split across frames, and the read-path dedupe rule: the snapshot
// path's skip, two credentials of one rule, a snapshot's match consumed by the
// next chunk, and a pending key expiring after a chunk with no match.
func TestAnnotationMatchesTheReferenceCorpus(t *testing.T) {
	g := loadSessionRecordingGolden(t)
	store := recording.NewInMemoryStore()
	rec := newSessionRecorder(g.SessionID, store, goldenConfig(t, nil), annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	compareRecorded(t, "annotated", replayScript(t, g, g.AnnotatedScript, rec, store), g.Recorded["annotated"])
}

// annotatedRecorder is a recording recorder with the reference's detector.
func annotatedRecorder(t *testing.T) (*sessionRecorder, *recording.InMemoryStore) {
	t.Helper()
	store := recording.NewInMemoryStore()
	rec := newSessionRecorder("s1", store, testRecordingConfig(), annotation.NewPatternDetector(nil), nil)
	rec.setEnabled(true)
	return rec, store
}

func descriptions(t *testing.T, store recording.Store) []string {
	t.Helper()
	var out []string
	for _, e := range entries(t, store, "annotation") {
		out = append(out, e["data"].(map[string]any)["description"].(string))
	}
	return out
}

const testAWSKey = "AKIA0123456789AB" // pragma: allowlist secret

// Mirrors tests/server/test_output_annotation.py: streamed output is scanned
// through its escape sequences...
func TestStreamedOutputIsScannedThroughItsEscapeSequences(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "\x1b[12;5H\x1b[38;2;255;176;0m" + testAWSKey + "\x1b[0m"})
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); !reflect.DeepEqual(got, []string{"AWS access key detected in read"}) {
		t.Fatalf("annotations = %v", got)
	}
}

// ...a match split across frames is found once...
func TestAMatchSplitAcrossFramesIsFoundOnce(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "DROP TA"})
	sendFrame(t, rec, map[string]any{"type": "term", "data": "BLE callers;"})
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); !reflect.DeepEqual(got, []string{"SQL DROP statement detected: DROP TABLE"}) {
		t.Fatalf("annotations = %v", got)
	}
}

// ...and nothing is scanned when nothing is recorded: annotations are
// recording entries, with nowhere to go otherwise.
func TestNothingIsScannedWhenNothingIsRecorded(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.setEnabled(false)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "DROP TA"})
	rec.setEnabled(true)
	rec.AttemptEnded(nil)
	rec.AttemptStarted()
	// Had the unrecorded half been carried, this would complete it.
	sendFrame(t, rec, map[string]any{"type": "term", "data": "BLE callers;"})
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); len(got) != 0 {
		t.Fatalf("unrecorded output was scanned: %v", got)
	}
}

// The streams and the sequence last the runtime's life, as the reference keeps
// them on HostedSessionRuntime: a partial match carried from one recording
// completes in the next, once per direction, and spans carry on from the
// sequence the first recording reached.
func TestAnnotationStateOutlivesARecording(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, snapshotFrame("$ "))
	must(t, rec.InputReceived("DROP TA"))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "DROP TA"})
	rec.AttemptEnded(nil)

	rec.AttemptStarted()
	must(t, rec.InputReceived("BLE x;"))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "BLE x;"})
	rec.AttemptEnded(nil)

	anns := entries(t, store, "annotation")
	if len(anns) != 2 {
		t.Fatalf("want one DROP TABLE per direction, got %v", anns)
	}
	for _, a := range anns {
		span := a["data"].(map[string]any)["span"].(map[string]any)
		if span["from_seq"] != float64(3) || span["to_seq"] != float64(3) {
			t.Fatalf("span = %v, want seq 3 carried over the restart", span)
		}
	}
}

// A registry's sessions annotate with the detector it was given.
func TestRegistryRecordersShareTheDetector(t *testing.T) {
	r := newTestRegistry(t)
	det := annotation.NewPatternDetector(nil)
	r.SetRecording(recording.NewInMemoryStore(), det)
	r.mu.Lock()
	defer r.mu.Unlock()
	rec := r.recorderFor(r.entries["provide-shell"])
	if rec.detector != det || rec.sendStream == nil || rec.readStream == nil || rec.sendStream == rec.readStream {
		t.Fatal("a session's recorder uses the registry's detector with a stream per direction")
	}
}

// --- read-path text, escape carry and dedupe (runtime.py _log_snapshot,
// _scan_output; runtime_helpers.py _split_incomplete_escape,
// _read_annotation_key) ----------------------------------------------------

// A rendered prompt ends its row with a reset. The screen is read as text, so
// the reset does not hide the colon and the secret typed next is masked.
func TestAStyledPasswordPromptMasksTheInput(t *testing.T) {
	rec, store := newTestRecorder(t, testRecordingConfig())
	rec.AttemptStarted()
	styled := "login: tim\n\x1b[1mPassword:\x1b[0m"
	sendFrame(t, rec, snapshotFrame(styled))
	must(t, rec.InputReceived("hunter2\r"))
	rec.AttemptEnded(nil)
	send := entries(t, store, "send")[0]["data"].(map[string]any)
	if send["masked"] != true || send["keys"] != "***" || send["byte_count"] != float64(8) {
		t.Fatalf("input after a styled prompt was not masked: %v", send)
	}
	raw, _ := json.Marshal(entries(t, store, ""))
	if strings.Contains(string(raw), "hunter2") {
		t.Fatalf("the secret reached the recording: %s", raw)
	}
	// The "read" entry keeps the screen as sent, escape sequences and all.
	if got := entries(t, store, "read")[0]["data"].(map[string]any)["screen"]; got != styled {
		t.Fatalf("read entry screen = %q, want the screen as sent", got)
	}
}

// The prompt test itself sees text: what logSnapshot hands it is stripped.
func TestPasswordPromptMustEndTheScreen(t *testing.T) {
	for text, want := range map[string]bool{
		"Password:":                   true,
		"PASSWORD:   \n\n":            true,
		"Password: ok\n$ ":            false,
		"password reset\nuser:":       false,
		"Your passphrase (again) :\t": true,
		"no prompt here":              false,
	} {
		if got := atPasswordPrompt(text); got != want {
			t.Errorf("atPasswordPrompt(%q) = %t, want %t", text, got, want)
		}
	}
}

// A styled match on a screen is found: SGR codes no longer split "sudo" from
// the rule.
func TestAStyledScreenIsScannedAsText(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, snapshotFrame("# \x1b[1msudo\x1b[0m rm\n# "))
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); !reflect.DeepEqual(got, []string{"sudo command detected: sudo"}) {
		t.Fatalf("annotations = %v", got)
	}
}

// A sequence cut between two frames is held back and joined to the next, so
// the second frame does not leave "msudo" behind for \bsudo\b to miss.
func TestAnEscapeSequenceSplitAcrossFramesIsCarried(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "ok \x1b[1"})
	rec.mu.Lock()
	carry := rec.escapeCarry
	rec.mu.Unlock()
	if carry != "\x1b[1" {
		t.Fatalf("escapeCarry = %q, want the cut sequence", carry)
	}
	sendFrame(t, rec, map[string]any{"type": "term", "data": "msudo reboot\x1b[0m\r\n"})
	rec.AttemptEnded(nil)
	want := []string{"sudo command detected: sudo", "reboot command detected: reboot"}
	if got := descriptions(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("annotations = %v, want %v", got, want)
	}
}

// The stream records every match and the snapshot path skips one it already
// holds; identical snapshots record a match once; a snapshot's match is
// consumed by the very next chunk only, and a later repeat is recorded.
func TestTheSnapshotPathDedupesAndTheNextChunkConsumes(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, map[string]any{"type": "term", "data": "$ sudo ls\r\n"})
	sendFrame(t, rec, snapshotFrame("$ sudo ls\n$ "))
	sendFrame(t, rec, snapshotFrame("$ shutdown now\n$ "))
	sendFrame(t, rec, snapshotFrame("$ shutdown now\n$ "))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "$ shutdown now\r\n"})
	sendFrame(t, rec, map[string]any{"type": "term", "data": "$ shutdown now\r\n"})
	rec.AttemptEnded(nil)
	want := []string{
		"sudo command detected: sudo",
		"shutdown command detected: shutdown",
		"shutdown command detected: shutdown",
	}
	if got := descriptions(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("annotations = %v, want %v", got, want)
	}
}

// Send-path matches are not read-path keys: the snapshot of a command typed
// is still annotated on the read path.
func TestSendPathMatchesDoNotDedupeTheScreen(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	must(t, rec.InputReceived("sudo ls\r"))
	sendFrame(t, rec, snapshotFrame("$ sudo ls\n$ "))
	rec.AttemptEnded(nil)
	if got := descriptions(t, store); len(got) != 2 {
		t.Fatalf("annotations = %v, want one per path", got)
	}
}

// An unrecorded snapshot is not annotated, so it remembers nothing either.
func TestAnUnrecordedSnapshotRemembersNothing(t *testing.T) {
	rec, _ := annotatedRecorder(t)
	sendFrame(t, rec, snapshotFrame("$ sudo ls"))
	if len(rec.readAnnotationKeys) != 0 {
		t.Fatalf("keys = %v", rec.readAnnotationKeys)
	}
}

// Both belong to one recording: the next starts with neither the keys nor a
// half-sequence from the last.
func TestReadPathStateIsResetWithTheRecording(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, snapshotFrame("$ sudo ls"))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "\x1b[1"})
	rec.AttemptEnded(nil)
	rec.mu.Lock()
	if rec.escapeCarry != "" || len(rec.readAnnotationKeys) != 0 || len(rec.snapshotOnlyKeys) != 0 {
		t.Fatalf("carry %q, keys %v, pending %v survived the recording",
			rec.escapeCarry, rec.readAnnotationKeys, rec.snapshotOnlyKeys)
	}
	rec.mu.Unlock()
	rec.AttemptStarted()
	// Carried, "\x1b[1" + "msudo" would strip to "sudo" and match.
	sendFrame(t, rec, map[string]any{"type": "term", "data": "msudo\r\n"})
	sendFrame(t, rec, snapshotFrame("$ sudo ls"))
	rec.AttemptEnded(nil)
	want := []string{"sudo command detected: sudo", "sudo command detected: sudo"}
	if got := descriptions(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("annotations = %v, want the screen annotated once per recording", got)
	}
}

// The set holds maxReadAnnotationKeys keys, and is cleared to make room for
// the next one past that.
func TestReadAnnotationKeysAreBounded(t *testing.T) {
	rec, _ := annotatedRecorder(t)
	for i := range maxReadAnnotationKeys {
		rec.rememberReadAnnotation(fmt.Sprint(i))
	}
	if len(rec.readAnnotationKeys) != maxReadAnnotationKeys {
		t.Fatalf("len = %d, want the bound held", len(rec.readAnnotationKeys))
	}
	rec.rememberReadAnnotation("0")
	if len(rec.readAnnotationKeys) != 1 {
		t.Fatalf("len = %d, want the full set cleared before the next key", len(rec.readAnnotationKeys))
	}
	if _, ok := rec.readAnnotationKeys["0"]; !ok {
		t.Fatal("the key that overflowed the set was not kept")
	}
	if maxReadAnnotationKeys != 1024 || screen.MaxEscapeCarry != 64 {
		t.Fatal("bounds drifted from runtime_helpers.py")
	}
}

// The key is the detector's match key: a digest of rule id and the full
// match, holding no raw text.
func TestReadAnnotationKey(t *testing.T) {
	anns := annotation.NewPatternDetector(nil).Detect("read", awsKeyA, 1)
	if len(anns) != 1 {
		t.Fatalf("annotations = %v", anns)
	}
	key := readAnnotationKey(anns[0])
	if key != annotation.AnnotationMatchKey("cred.aws_access_key", "AKIAIOSFODNN7EXA") ||
		strings.Contains(key, "AKIA") || len(key) != 64 {
		t.Fatalf("key = %q", key)
	}
}

// Every new match on one screen or in one chunk is recorded, not only the
// first: on every path the loop runs on past a write that succeeded.
func TestEveryMatchInOneStepIsRecorded(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	sendFrame(t, rec, snapshotFrame("$ rm -rf build\n$ shutdown now\n$ "))
	sendFrame(t, rec, map[string]any{"type": "term", "data": "$ sudo reboot\r\n"})
	must(t, rec.InputReceived("sudo reboot\r"))
	rec.AttemptEnded(nil)
	want := []string{
		"Recursive force-remove detected: rm -rf",
		"shutdown command detected: shutdown",
		"sudo command detected: sudo",
		"reboot command detected: reboot",
		"sudo command detected: sudo",
		"reboot command detected: reboot",
	}
	if got := descriptions(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("annotations = %v, want %v", got, want)
	}
}

// --- read-path dedupe rule (tests/server/test_read_path_dedupe.py) ---------

const (
	awsKeyA   = "AKIAIOSFODNN7EXAMPLE" // pragma: allowlist secret
	awsKeyB   = "AKIAJ2345678ABCDWXYZ" // pragma: allowlist secret
	descSudo  = "sudo command detected: sudo"
	descRM    = "Recursive force-remove detected: rm -rf"
	descAWS   = "AWS access key detected in read"
	descReboo = "reboot command detected: reboot"
)

func term(t *testing.T, rec *sessionRecorder, data string) {
	t.Helper()
	sendFrame(t, rec, map[string]any{"type": "term", "data": data})
}

func snap(t *testing.T, rec *sessionRecorder, text string) {
	t.Helper()
	sendFrame(t, rec, snapshotFrame(text))
}

// dedupeCase runs steps in one open recording and checks what was annotated.
func dedupeCase(t *testing.T, want []string, steps func(*sessionRecorder)) *sessionRecorder {
	t.Helper()
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	steps(rec)
	must(t, rec.flush())
	if got := descriptions(t, store); !reflect.DeepEqual(got, want) {
		t.Fatalf("annotations = %v, want %v", got, want)
	}
	return rec
}

func TestASnapshotAfterTheStreamDoesNotRepeatItsMatch(t *testing.T) {
	dedupeCase(t, []string{descSudo}, func(rec *sessionRecorder) {
		term(t, rec, "$ sudo ls\r\n")
		snap(t, rec, "$ sudo ls\n")
	})
}

func TestRepeatedIdenticalSnapshotsAnnotateOnce(t *testing.T) {
	dedupeCase(t, []string{descRM}, func(rec *sessionRecorder) {
		for range 3 {
			snap(t, rec, "$ rm -rf build\n")
		}
	})
}

func TestACommandStreamedTwiceIsAnnotatedTwice(t *testing.T) {
	dedupeCase(t, []string{descSudo, descSudo}, func(rec *sessionRecorder) {
		term(t, rec, "$ sudo ls\r\n")
		term(t, rec, "$ sudo ls\r\n")
		snap(t, rec, "$ sudo ls\n$ sudo ls\n")
	})
}

func TestASnapshotStillRecordsWhatTheStreamNeverSaw(t *testing.T) {
	dedupeCase(t, []string{descSudo, descRM}, func(rec *sessionRecorder) {
		term(t, rec, "$ sudo ls\r\n")
		snap(t, rec, "$ sudo ls\n$ rm -rf build\n")
	})
}

// Snapshot first, then the same text on the stream: one record. The stream
// consumes the snapshot's record once; a later repeat is a new occurrence.
func TestAStreamMatchASnapshotAlreadyRecordedIsNotRecordedAgain(t *testing.T) {
	rec, store := annotatedRecorder(t)
	rec.AttemptStarted()
	snap(t, rec, "$ sudo ls\n")
	if len(rec.snapshotOnlyKeys) != 1 {
		t.Fatalf("pending = %v, want the snapshot's key", rec.snapshotOnlyKeys)
	}
	term(t, rec, "$ sudo ls\r\n")
	if len(rec.snapshotOnlyKeys) != 0 {
		t.Fatalf("pending = %v after the chunk", rec.snapshotOnlyKeys)
	}
	must(t, rec.flush())
	if got := descriptions(t, store); !reflect.DeepEqual(got, []string{descSudo}) {
		t.Fatalf("annotations = %v", got)
	}
	term(t, rec, "$ sudo ls\r\n")
	snap(t, rec, "$ sudo ls\n$ sudo ls\n")
	must(t, rec.flush())
	if got := descriptions(t, store); !reflect.DeepEqual(got, []string{descSudo, descSudo}) {
		t.Fatalf("annotations = %v", got)
	}
}

func TestASnapshotThenItsChunkRecordsOnceAndALaterNewMatchRecordsAgain(t *testing.T) {
	dedupeCase(t, []string{descSudo, descSudo, descReboo}, func(rec *sessionRecorder) {
		snap(t, rec, "$ sudo ls\n")
		term(t, rec, "$ sudo ls\r\n")
		term(t, rec, "$ sudo reboot\r\n")
	})
}

// Missing an annotation is worse than an occasional duplicate: the pending
// key dedupes only the chunk right after the snapshot.
func TestPendingSnapshotKeysExpireWithTheNextChunkEvenWithoutAMatch(t *testing.T) {
	dedupeCase(t, []string{descSudo, descSudo}, func(rec *sessionRecorder) {
		snap(t, rec, "$ sudo ls\n")
		term(t, rec, "unrelated output\r\n")
		if len(rec.snapshotOnlyKeys) != 0 {
			t.Fatalf("pending = %v after a chunk", rec.snapshotOnlyKeys)
		}
		term(t, rec, "$ sudo ls\r\n")
	})
}

func TestAPendingKeyIsConsumedOnlyByItsOwnMatch(t *testing.T) {
	rec := dedupeCase(t, []string{descSudo, descRM}, func(rec *sessionRecorder) {
		snap(t, rec, "$ sudo ls\n")
		term(t, rec, "$ rm -rf build\r\n")
	})
	if len(rec.snapshotOnlyKeys) != 0 {
		t.Fatalf("pending = %v", rec.snapshotOnlyKeys)
	}
}

func TestAnEmptyChunkLeavesPendingKeysAlone(t *testing.T) {
	dedupeCase(t, []string{descSudo}, func(rec *sessionRecorder) {
		snap(t, rec, "$ sudo ls\n")
		term(t, rec, "")
		term(t, rec, "$ sudo ls\r\n")
	})
}

// Output while not recording is not scanned, and leaves pending keys alone.
func TestAnUnrecordedChunkLeavesPendingKeysAlone(t *testing.T) {
	rec, _ := annotatedRecorder(t)
	rec.snapshotOnlyKeys["k"] = struct{}{}
	term(t, rec, "$ sudo ls\r\n")
	if _, ok := rec.snapshotOnlyKeys["k"]; !ok {
		t.Fatal("an unrecorded chunk cleared the pending keys")
	}
}

// Credential rules never embed the match in the description, so a key of
// label+description would have dropped every AWS key after the first.
func TestDifferentCredentialsOfOneRuleInSuccessiveSnapshotsAreAllRecorded(t *testing.T) {
	dedupeCase(t, []string{descAWS, descAWS}, func(rec *sessionRecorder) {
		snap(t, rec, awsKeyA+" first\n")
		snap(t, rec, awsKeyB+" second\n")
		snap(t, rec, awsKeyB+" second\n")
	})
}

func TestDifferentCredentialsOnTheStreamAfterASnapshotAreRecorded(t *testing.T) {
	rec := dedupeCase(t, []string{descAWS, descAWS}, func(rec *sessionRecorder) {
		snap(t, rec, awsKeyA+"\n")
		term(t, rec, awsKeyB+"\r\n")
	})
	// The key sets hold digests, never the secret.
	for k := range rec.readAnnotationKeys {
		if strings.Contains(k, "AKIA") || len(k) != 64 {
			t.Fatalf("key %q is not a digest", k)
		}
	}
	if len(rec.readAnnotationKeys) != 2 {
		t.Fatalf("keys = %v, want one per credential", rec.readAnnotationKeys)
	}
}

// The snapshot-only set is bounded as the other is.
func TestTheSnapshotOnlySetIsBoundedToo(t *testing.T) {
	keys := map[string]struct{}{}
	for i := range maxReadAnnotationKeys {
		addBounded(keys, fmt.Sprint("k", i))
	}
	if len(keys) != maxReadAnnotationKeys {
		t.Fatalf("len = %d, want the bound held", len(keys))
	}
	addBounded(keys, "next")
	if _, ok := keys["next"]; !ok || len(keys) != 1 {
		t.Fatalf("keys = %v, want only the next key", keys)
	}
}
