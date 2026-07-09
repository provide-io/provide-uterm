//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sessionlogger

import (
	"encoding/base64"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/redaction"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
)

// failingStore wraps InMemoryStore and fails AppendEvents while failing is
// set.
type failingStore struct {
	*recording.InMemoryStore
	mu      sync.Mutex
	failing bool
	calls   int
}

func (s *failingStore) setFailing(v bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.failing = v
}

func (s *failingStore) AppendEvents(sessionID string, events []recording.Event) error {
	s.mu.Lock()
	failing := s.failing
	s.calls++
	s.mu.Unlock()
	if failing {
		return errors.New("store down")
	}
	return s.InMemoryStore.AppendEvents(sessionID, events)
}

func newLogger(t *testing.T, store recording.Store, opts Options) *SessionLogger {
	t.Helper()
	if opts.FlushInterval == 0 {
		opts.FlushInterval = time.Hour // keep the periodic flusher quiet
	}
	l := New(store, opts)
	if err := l.Start("sess-1"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = l.Stop() })
	return l
}

func entries(t *testing.T, store recording.Store, event string) []recording.Event {
	t.Helper()
	got, err := store.GetEntries("sess-1", recording.Query{Event: event, Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	return got
}

func TestLogSendRecordsRedactedKeysAndBytes(t *testing.T) {
	store := recording.NewInMemoryStore()
	redactor, err := redaction.MakeRedactor([]string{`secret\w+`})
	if err != nil {
		t.Fatal(err)
	}
	l := newLogger(t, store, Options{Redactor: redactor})
	if err := l.LogSend("login secretpass\r"); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	sends := entries(t, store, "send")
	if len(sends) != 1 {
		t.Fatalf("sends = %v", sends)
	}
	data := sends[0]["data"].(map[string]any)
	if data["keys"] != "login [REDACTED]\r" {
		t.Fatalf("keys = %q", data["keys"])
	}
	raw, err := base64.StdEncoding.DecodeString(data["bytes_b64"].(string))
	if err != nil || string(raw) != "login [REDACTED]\r" {
		t.Fatalf("bytes = %q err %v", raw, err)
	}
}

func TestLogSendMasked(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{})
	if err := l.LogSendMasked(9); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	data := entries(t, store, "send")[0]["data"].(map[string]any)
	if data["keys"] != "***" || data["masked"] != true || data["byte_count"] != 9 {
		t.Fatalf("data = %v", data)
	}
}

func TestLogScreenIncludesSnapshotAndRaw(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{})
	snap := session.Snapshot{
		Screen: "hello", ScreenHash: "h", Cols: 80, Rows: 25, Term: "ANSI",
		PromptDetected: &session.PromptDetection{PromptID: "p", IsIdle: true, KVData: map[string]any{"a": "b"}},
	}
	if err := l.LogScreen(snap, []byte{0xC9, 0xCD}); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	data := entries(t, store, "read")[0]["data"].(map[string]any)
	if data["screen"] != "hello" || data["cols"] != 80 {
		t.Fatalf("data = %v", data)
	}
	if data["raw"] != "╔═" {
		t.Fatalf("raw = %q", data["raw"])
	}
	raw, _ := base64.StdEncoding.DecodeString(data["raw_bytes_b64"].(string))
	if string(raw) != string([]byte{0xC9, 0xCD}) {
		t.Fatalf("raw bytes = %v", raw)
	}
	pd := data["prompt_detected"].(map[string]any)
	if pd["prompt_id"] != "p" || pd["is_idle"] != true {
		t.Fatalf("prompt_detected = %v", pd)
	}
}

func TestWireModeGating(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{}) // exclude mode
	if err := l.LogWire("send", "chunk"); err != nil {
		t.Fatal(err)
	}
	if err := l.LogControl("recv", map[string]any{"type": "ping"}); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	if got := entries(t, store, "wire_send"); len(got) != 0 {
		t.Fatalf("wire logged in exclude mode: %v", got)
	}

	store2 := recording.NewInMemoryStore()
	l2 := newLogger(t, store2, Options{ControlChannelMode: ModeWire})
	if err := l2.LogWire("send", "chunk"); err != nil {
		t.Fatal(err)
	}
	if err := l2.LogControl("recv", map[string]any{"type": "ping"}); err != nil {
		t.Fatal(err)
	}
	if err := l2.Flush(); err != nil {
		t.Fatal(err)
	}
	wire := entries(t, store2, "wire_send")
	if len(wire) != 1 || wire[0]["data"].(map[string]any)["text"] != "chunk" {
		t.Fatalf("wire = %v", wire)
	}
	ctrl := entries(t, store2, "control_recv")
	if len(ctrl) != 1 {
		t.Fatalf("ctrl = %v", ctrl)
	}
}

func TestContextAttachedAndCleared(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{})
	l.SetContext(map[string]string{"game": "tw2002"})
	if err := l.LogEvent("custom", map[string]any{"x": 1}); err != nil {
		t.Fatal(err)
	}
	l.ClearContext()
	if err := l.LogEvent("custom", map[string]any{"x": 2}); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	got := entries(t, store, "custom")
	if got[0]["ctx"].(map[string]string)["game"] != "tw2002" {
		t.Fatalf("ctx = %v", got[0])
	}
	if _, ok := got[1]["ctx"]; ok {
		t.Fatalf("ctx not cleared: %v", got[1])
	}
}

func TestQuotaSuppressesWritesAndWarnsOnce(t *testing.T) {
	// White-box: skip Start() (whose log_start size would vary with the
	// timestamp) and drive the quota boundary directly. The first write is
	// below the quota and passes, pushing usage over; the rest are suppressed
	// with a single warning.
	store := recording.NewInMemoryStore()
	var buf strings.Builder
	logger := slog.New(slog.NewTextHandler(&buf, nil))
	l := New(store, Options{MaxBytes: 10, Logger: logger})
	l.sessionID = "sess-1"
	for range 3 {
		if err := l.LogEvent("e", map[string]any{}); err != nil {
			t.Fatal(err)
		}
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	if got := entries(t, store, "e"); len(got) != 1 {
		t.Fatalf("events = %v", got)
	}
	if strings.Count(buf.String(), "session_logger_quota_reached") != 1 {
		t.Fatalf("warnings: %q", buf.String())
	}
}

func TestBatchSizeTriggersFlush(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{BatchSize: 2})
	if err := l.LogEvent("e", map[string]any{"n": 1}); err != nil {
		t.Fatal(err)
	}
	if got := entries(t, store, "e"); len(got) != 0 {
		t.Fatalf("flushed early: %v", got)
	}
	if err := l.LogEvent("e", map[string]any{"n": 2}); err != nil {
		t.Fatal(err)
	}
	if got := entries(t, store, "e"); len(got) != 2 {
		t.Fatalf("batch not flushed: %v", got)
	}
}

func TestFailedFlushKeepsBatch(t *testing.T) {
	store := &failingStore{InMemoryStore: recording.NewInMemoryStore()}
	l := newLogger(t, store, Options{})
	store.setFailing(true)
	if err := l.LogEvent("e", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err == nil {
		t.Fatal("expected flush error")
	}
	store.setFailing(false)
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	if got := entries(t, store, "e"); len(got) != 1 {
		t.Fatalf("events = %v", got)
	}
}

func TestPeriodicFlushRetriesAndWarns(t *testing.T) {
	store := &failingStore{InMemoryStore: recording.NewInMemoryStore()}
	var mu sync.Mutex
	var buf strings.Builder
	logger := slog.New(slog.NewTextHandler(lockedWriter{&mu, &buf}, nil))
	l := New(store, Options{FlushInterval: 5 * time.Millisecond, Logger: logger})
	if err := l.Start("sess-1"); err != nil {
		t.Fatal(err)
	}
	store.setFailing(true)
	if err := l.LogEvent("e", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	// Wait for at least one failed periodic attempt.
	deadline := time.Now().Add(2 * time.Second)
	for {
		mu.Lock()
		warned := strings.Contains(buf.String(), "session_logger_periodic_flush_failed")
		mu.Unlock()
		if warned || time.Now().After(deadline) {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	store.setFailing(false)
	if err := l.Stop(); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	warned := strings.Contains(buf.String(), "session_logger_periodic_flush_failed")
	mu.Unlock()
	if !warned {
		t.Fatal("periodic flush failure was not logged")
	}
	// The batch survived the outage and flushed on Stop.
	if got := entries(t, store, "e"); len(got) != 1 {
		t.Fatalf("events = %v", got)
	}
}

type lockedWriter struct {
	mu  *sync.Mutex
	buf *strings.Builder
}

func (w lockedWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.buf.Write(p)
}

func TestStartErrors(t *testing.T) {
	blocked := recording.NewLocalFileStore("/dev/null/nope")
	l := New(blocked, Options{})
	if err := l.Start("s"); err == nil {
		t.Fatal("expected error")
	}
}

// metaFailStore fails RecordingMeta.
type metaFailStore struct {
	*recording.InMemoryStore
}

func (metaFailStore) RecordingMeta(string) (recording.Meta, error) {
	return recording.Meta{}, errors.New("meta down")
}

func TestStartMetaError(t *testing.T) {
	l := New(metaFailStore{recording.NewInMemoryStore()}, Options{})
	if err := l.Start("s"); err == nil || err.Error() != "meta down" {
		t.Fatalf("err = %v", err)
	}
}

func TestStopReturnsFlushError(t *testing.T) {
	store := &failingStore{InMemoryStore: recording.NewInMemoryStore()}
	l := New(store, Options{FlushInterval: time.Hour})
	if err := l.Start("sess-1"); err != nil {
		t.Fatal(err)
	}
	if err := l.LogEvent("e", map[string]any{}); err != nil {
		t.Fatal(err)
	}
	store.setFailing(true)
	if err := l.Stop(); err == nil {
		t.Fatal("expected flush error from Stop")
	}
}

func TestStopWithoutStart(t *testing.T) {
	l := New(recording.NewInMemoryStore(), Options{})
	if err := l.Stop(); err != nil {
		t.Fatal(err)
	}
}

func TestWriteEventUnserializable(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{})
	if err := l.LogEvent("bad", map[string]any{"ch": make(chan int)}); err == nil {
		t.Fatal("expected marshal error")
	}
}

func TestRedactValueNestedInKVData(t *testing.T) {
	store := recording.NewInMemoryStore()
	redactor, err := redaction.MakeRedactor([]string{"token"})
	if err != nil {
		t.Fatal(err)
	}
	l := newLogger(t, store, Options{Redactor: redactor})
	snap := session.Snapshot{
		PromptDetected: &session.PromptDetection{
			KVData: map[string]any{
				"s":    "token",
				"list": []any{"token", float64(1), map[string]any{"inner": "token"}},
				"n":    float64(2),
			},
		},
	}
	if err := l.LogScreen(snap, nil); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	kv := entries(t, store, "read")[0]["data"].(map[string]any)["prompt_detected"].(map[string]any)["kv_data"].(map[string]any)
	if kv["s"] != "[REDACTED]" {
		t.Fatalf("kv = %v", kv)
	}
	list := kv["list"].([]any)
	if list[0] != "[REDACTED]" || list[1] != float64(1) || list[2].(map[string]any)["inner"] != "[REDACTED]" {
		t.Fatalf("list = %v", list)
	}
	if kv["n"] != float64(2) {
		t.Fatalf("kv = %v", kv)
	}
}
