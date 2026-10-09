//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
)

// fakeRecorder is a webhook recording endpoint that keeps what it is sent.
type fakeRecorder struct {
	mu       sync.Mutex
	posts    []map[string]any
	gets     []string
	auth     []string
	metaBody string
	entries  string
	status   int
}

func (f *fakeRecorder) handler(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.auth = append(f.auth, r.Header.Get("Authorization"))
	if r.Method == http.MethodPost {
		var body map[string]any
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		f.posts = append(f.posts, body)
		return
	}
	f.gets = append(f.gets, r.URL.RequestURI())
	if f.status != 0 {
		w.WriteHeader(f.status)
		return
	}
	switch r.URL.Path {
	case "/rec/s1/meta":
		_, _ = io.WriteString(w, f.metaBody)
	case "/rec/s1/entries":
		_, _ = io.WriteString(w, f.entries)
	}
}

func newWebhookStore(t *testing.T, f *fakeRecorder, secret string) *WebhookRecordingStore {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(f.handler))
	t.Cleanup(srv.Close)
	return NewWebhookRecordingStore(srv.URL+"/rec", secret, time.Second, NewEgressGuard(nil, nil))
}

var _ recording.Store = (*WebhookRecordingStore)(nil)

// The lifecycle is POSTed to the URL as the reference posts it:
// {"session_id", "action", ...payload}, with the secret as a bearer token.
func TestWebhookStorePostsTheLifecycle(t *testing.T) {
	f := &fakeRecorder{}
	s := newWebhookStore(t, f, "tok")
	if err := s.StartSession("s1", map[string]any{"started_at": 1.5}); err != nil {
		t.Fatal(err)
	}
	if err := s.AppendEvents("s1", []recording.Event{{"ts": 2.5, "event": "send", "data": map[string]any{"keys": "x"}}}); err != nil {
		t.Fatal(err)
	}
	if err := s.EndSession("s1"); err != nil {
		t.Fatal(err)
	}
	want := []map[string]any{
		{"session_id": "s1", "action": "start", "metadata": map[string]any{"started_at": 1.5}},
		{"session_id": "s1", "action": "append", "events": []any{map[string]any{"ts": 2.5, "event": "send", "data": map[string]any{"keys": "x"}}}},
		{"session_id": "s1", "action": "end"},
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if !reflect.DeepEqual(f.posts, want) {
		t.Fatalf("posts = %v, want %v", f.posts, want)
	}
	for _, a := range f.auth {
		if a != "Bearer tok" {
			t.Fatalf("authorization = %q", a)
		}
	}
}

// Reads GET {url}/{session_id}/{action}: meta as the endpoint answers it,
// entries from its "entries" key, with the query passed through.
func TestWebhookStoreReads(t *testing.T) {
	f := &fakeRecorder{
		metaBody: `{"session_id":"s1","exists":true,"size_bytes":42}`,
		entries:  `{"entries":[{"event":"read","data":{}}]}`,
	}
	s := newWebhookStore(t, f, "")
	meta, err := s.RecordingMeta("s1")
	if err != nil || meta.SessionID != "s1" || !meta.Exists || meta.SizeBytes != 42 {
		t.Fatalf("meta = %+v, %v", meta, err)
	}
	off := 3
	got, err := s.GetEntries("s1", recording.Query{Limit: 5, Offset: &off, Event: "read"})
	if err != nil || len(got) != 1 || got[0]["event"] != "read" {
		t.Fatalf("entries = %v, %v", got, err)
	}
	if _, err := s.GetEntries("s1", recording.Query{}); err != nil {
		t.Fatal(err)
	}
	path, err := s.GetPath("s1")
	if err != nil || path != "" {
		t.Fatalf("path = %q, %v; a webhook store has no local file", path, err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	want := []string{"/rec/s1/meta", "/rec/s1/entries?event=read&limit=5&offset=3", "/rec/s1/entries?limit=200"}
	if !reflect.DeepEqual(f.gets, want) {
		t.Fatalf("gets = %v, want %v", f.gets, want)
	}
	for _, a := range f.auth {
		if a != "" {
			t.Fatalf("authorization sent without a secret: %q", a)
		}
	}
}

// Everything is best effort, as in the reference: an endpoint that fails, is
// unreachable, answers non-200, or answers something unusable reads as an
// empty recording, and writes never fail.
func TestWebhookStoreIsBestEffort(t *testing.T) {
	missing := recording.Meta{SessionID: "s1"}
	for name, f := range map[string]*fakeRecorder{
		"non-200":     {status: http.StatusInternalServerError},
		"empty meta":  {metaBody: `{}`, entries: `{}`},
		"not json":    {metaBody: `nope`, entries: `nope`},
		"not a dict":  {metaBody: `[1]`, entries: `[1]`},
		"bad entries": {metaBody: `null`, entries: `{"entries":"x"}`},
		"bad meta":    {metaBody: `{"exists":"yes"}`, entries: `{}`},
	} {
		s := newWebhookStore(t, f, "")
		if meta, err := s.RecordingMeta("s1"); err != nil || meta != missing {
			t.Errorf("%s: meta = %+v, %v", name, meta, err)
		}
		if got, err := s.GetEntries("s1", recording.Query{}); err != nil || len(got) != 0 {
			t.Errorf("%s: entries = %v, %v", name, got, err)
		}
	}

	// An event JSON cannot carry is dropped, not sent half-written.
	f := &fakeRecorder{}
	s := newWebhookStore(t, f, "")
	if err := s.AppendEvents("s1", []recording.Event{{"bad": make(chan int)}}); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	if len(f.posts) != 0 {
		t.Fatalf("an unencodable batch was posted: %v", f.posts)
	}
	f.mu.Unlock()

	dead := NewWebhookRecordingStore("http://127.0.0.1:1/rec", "", time.Second, NewEgressGuard(nil, nil))
	if err := dead.StartSession("s1", nil); err != nil {
		t.Fatal(err)
	}
	if meta, _ := dead.RecordingMeta("s1"); meta != missing {
		t.Fatalf("unreachable meta = %+v", meta)
	}
}

// The SSRF guard runs before every request: a target the guard refuses is
// never contacted, and reads as an empty recording.
func TestWebhookStoreHonoursTheEgressGuard(t *testing.T) {
	f := &fakeRecorder{metaBody: `{"session_id":"s1","exists":true,"size_bytes":1}`}
	srv := httptest.NewServer(http.HandlerFunc(f.handler))
	defer srv.Close()
	refuse := NewEgressGuard(func(context.Context, string) ([]string, error) {
		return nil, errors.New("no such host")
	}, nil)
	s := NewWebhookRecordingStore("http://recorder.invalid/rec", "", time.Second, refuse)
	_ = s.StartSession("s1", nil)
	if meta, _ := s.RecordingMeta("s1"); meta.Exists {
		t.Fatal("a refused target was read")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.posts)+len(f.gets) != 0 {
		t.Fatal("a refused target was contacted")
	}
}

// A redirect is not followed, as the reference's client does not follow one.
func TestWebhookStoreDoesNotFollowRedirects(t *testing.T) {
	f := &fakeRecorder{metaBody: `{"session_id":"s1","exists":true,"size_bytes":1}`}
	target := httptest.NewServer(http.HandlerFunc(f.handler))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+r.URL.Path, http.StatusFound)
	}))
	defer redirect.Close()
	s := NewWebhookRecordingStore(redirect.URL+"/rec", "", time.Second, NewEgressGuard(nil, nil))
	if meta, _ := s.RecordingMeta("s1"); meta.Exists {
		t.Fatal("the redirect was followed")
	}
}
