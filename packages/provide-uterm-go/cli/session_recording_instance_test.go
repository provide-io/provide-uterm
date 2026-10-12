//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"reflect"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
)

// A session reusing an id (a PAM capture session named after a recycled pid)
// is never handed the earlier session's recording: recording reads are
// authorized against whoever holds the id now. A reconnect of the same session
// -- one recorder opening a recording per connection attempt -- continues it.
// Port of test_runtime_recording_instance.py.

const reusedID = "pam-suokki-capture-4242"

func oneAttempt(t *testing.T, rec *sessionRecorder, keys string) {
	t.Helper()
	rec.AttemptStarted()
	must(t, rec.InputReceived(keys))
	rec.AttemptEnded(nil)
}

func sentKeys(t *testing.T, store recording.Store) []string {
	t.Helper()
	var out []string
	for _, e := range entriesOf(t, store, reusedID, "send") {
		out = append(out, e["data"].(map[string]any)["keys"].(string))
	}
	return out
}

func TestReusedSessionIDDoesNotInheritTheRecording(t *testing.T) {
	stores := map[string]recording.Store{
		"file":   recording.NewLocalFileStore(t.TempDir()),
		"memory": recording.NewInMemoryStore(),
	}
	for name, store := range stores {
		t.Run(name, func(t *testing.T) {
			first := newSessionRecorder(reusedID, store, testRecordingConfig(), nil, nil)
			first.setEnabled(true)
			oneAttempt(t, first, "alice-1")
			oneAttempt(t, first, "alice-2")
			if got := sentKeys(t, store); !reflect.DeepEqual(got, []string{"alice-1", "alice-2"}) {
				t.Fatalf("a reconnect lost the session's recording: %v", got)
			}

			// The first session is gone; another caller's session takes the id.
			second := newSessionRecorder(reusedID, store, testRecordingConfig(), nil, nil)
			second.setEnabled(true)
			oneAttempt(t, second, "bob")
			if got := sentKeys(t, store); !reflect.DeepEqual(got, []string{"bob"}) {
				t.Fatalf("the new session was served %v, want only [bob]", got)
			}
		})
	}
}

func TestEachRecorderIsItsOwnRecordingInstance(t *testing.T) {
	store := recording.NewInMemoryStore()
	a := newSessionRecorder(reusedID, store, testRecordingConfig(), nil, nil)
	b := newSessionRecorder(reusedID, store, testRecordingConfig(), nil, nil)
	if a.instance == "" || a.instance == b.instance {
		t.Fatalf("instances %q and %q, want two distinct non-empty", a.instance, b.instance)
	}
}
