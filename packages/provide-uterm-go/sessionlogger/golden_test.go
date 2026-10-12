//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sessionlogger

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/redaction"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/session"
)

// The recording format, held to the Python reference.
//
// testdata/session_logger_golden.json is what CPython's SessionLogger writes
// for a fixed script, with only the fresh-by-design timestamps stripped. It is
// a copy of the TypeScript port's corpus (regenerate with
// packages/provide-uterm-ts/testdata/gen_session_logger_golden.py from the
// repository root); scripts/check_protocol_drift.py keeps the Go and C# copies
// identical to it, so all three ports are held to the same corpus. A Go
// recording that matches it is one every reader of a Python recording can read.

type loggerGolden struct {
	ExcludeMode []map[string]any `json:"exclude_mode"`
	WireMode    []map[string]any `json:"wire_mode"`
	Redacted    []map[string]any `json:"redacted"`
	Quota       struct {
		Entries []map[string]any `json:"entries"`
	} `json:"quota"`
	QuotaBoundary struct {
		MaxBytes int            `json:"max_bytes"`
		Payload  map[string]any `json:"payload"`
		Attempts int            `json:"attempts"`
		Written  int            `json:"written"`
	} `json:"quota_boundary"`
	Batch struct {
		AfterOne int `json:"after_one"`
		AfterTwo int `json:"after_two"`
	} `json:"batch"`
}

func loadLoggerGolden(t *testing.T) loggerGolden {
	t.Helper()
	raw, err := os.ReadFile("testdata/session_logger_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g loggerGolden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

// stripGolden drops the timestamps the generator drops, and normalises the
// entries through JSON so Go ints compare equal to decoded float64s.
func stripGolden(t *testing.T, entries []recording.Event) []map[string]any {
	t.Helper()
	raw, err := json.Marshal(entries)
	if err != nil {
		t.Fatal(err)
	}
	var out []map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	for _, item := range out {
		delete(item, "ts")
		if item["event"] == "log_start" {
			item["data"] = map[string]any{"stripped": true}
		}
	}
	return out
}

// driveGoldenScript runs the generator's fixed script through a Go logger.
func driveGoldenScript(t *testing.T, opts Options) []map[string]any {
	t.Helper()
	store := recording.NewInMemoryStore()
	opts.FlushInterval = time.Hour
	l := New(store, opts)
	steps := []func() error{
		func() error { return l.Start("s1") },
		func() error { return l.LogSend("ls -la\r") },
		func() error { return l.LogSendMasked(8) },
		func() error {
			return l.LogScreenFrame(map[string]any{"screen": "hello", "cursor": map[string]any{"x": 1}}, []byte("raw\xff"))
		},
		func() error { return l.LogEvent("custom", map[string]any{"a": 1}) },
		func() error { return l.LogWire("send", "wire out") },
		func() error { return l.LogWire("recv", "wire in") },
		func() error { return l.LogControl("send", map[string]any{"type": "hello"}) },
		func() error { return l.LogControl("recv", map[string]any{"type": "hello_ack"}) },
		func() error {
			l.SetContext(map[string]string{"worker": "w1", "n": "2"})
			return l.LogEvent("with_context", map[string]any{})
		},
		func() error {
			l.ClearContext()
			return l.LogEvent("without_context", map[string]any{})
		},
		l.Flush,
		l.Stop,
	}
	for i, step := range steps {
		if err := step(); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	entries, err := store.GetEntries("s1", recording.Query{Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	return stripGolden(t, entries)
}

func assertGolden(t *testing.T, name string, got, want []map[string]any) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		g, _ := json.MarshalIndent(got, "", " ")
		w, _ := json.MarshalIndent(want, "", " ")
		t.Fatalf("%s diverges from the Python golden\n got: %s\nwant: %s", name, g, w)
	}
}

func TestGoldenExcludeMode(t *testing.T) {
	g := loadLoggerGolden(t)
	assertGolden(t, "exclude_mode", driveGoldenScript(t, Options{}), g.ExcludeMode)
}

func TestGoldenWireMode(t *testing.T) {
	g := loadLoggerGolden(t)
	assertGolden(t, "wire_mode", driveGoldenScript(t, Options{ControlChannelMode: ModeWire}), g.WireMode)
}

func TestGoldenRedacted(t *testing.T) {
	g := loadLoggerGolden(t)
	redactor, err := redaction.MakeRedactor([]string{`secret\w*`})
	if err != nil {
		t.Fatal(err)
	}
	got := driveGoldenScript(t, Options{ControlChannelMode: ModeWire, Redactor: redactor})
	assertGolden(t, "redacted", got, g.Redacted)
}

func TestGoldenQuota(t *testing.T) {
	g := loadLoggerGolden(t)
	store := recording.NewInMemoryStore()
	l := New(store, Options{MaxBytes: 1, FlushInterval: time.Hour})
	if err := l.Start("s1"); err != nil {
		t.Fatal(err)
	}
	_ = l.LogEvent("first", map[string]any{"a": 1})
	_ = l.LogEvent("second", map[string]any{"a": 2})
	if err := l.Stop(); err != nil {
		t.Fatal(err)
	}
	entries, _ := store.GetEntries("s1", recording.Query{Limit: 500})
	assertGolden(t, "quota", stripGolden(t, entries), g.Quota.Entries)
}

// The quota is counted as the reference counts it, len(json.dumps(record)) + 1
// under CPython's defaults: ", " and ": " separators and \uXXXX escapes. The
// golden's budget runs out partway through the third entry of a payload whose
// compact and CPython encodings differ by far more than a timestamp's digits,
// so a port counting compact JSON writes a fourth.
func TestGoldenQuotaBoundary(t *testing.T) {
	g := loadLoggerGolden(t).QuotaBoundary
	store := recording.NewInMemoryStore()
	l := New(store, Options{MaxBytes: g.MaxBytes, FlushInterval: time.Hour})
	if err := l.Start("s1"); err != nil {
		t.Fatal(err)
	}
	for range g.Attempts {
		_ = l.LogEvent("e", g.Payload)
	}
	if err := l.Stop(); err != nil {
		t.Fatal(err)
	}
	entries, _ := store.GetEntries("s1", recording.Query{Event: "e", Limit: 500})
	if len(entries) != g.Written {
		t.Fatalf("wrote %d entries under the quota, the reference writes %d", len(entries), g.Written)
	}
}

func TestGoldenBatch(t *testing.T) {
	g := loadLoggerGolden(t)
	store := recording.NewInMemoryStore()
	l := New(store, Options{BatchSize: 2, FlushInterval: time.Hour})
	if err := l.Start("s1"); err != nil {
		t.Fatal(err)
	}
	_ = l.LogEvent("a", map[string]any{})
	before, _ := store.GetEntries("s1", recording.Query{Limit: 500})
	_ = l.LogEvent("b", map[string]any{})
	after, _ := store.GetEntries("s1", recording.Query{Limit: 500})
	_ = l.Stop()
	if len(before) != g.Batch.AfterOne || len(after) != g.Batch.AfterTwo {
		t.Fatalf("batch flush: got %d/%d, want %d/%d", len(before), len(after), g.Batch.AfterOne, g.Batch.AfterTwo)
	}
}

// payloadGolden is the caller-payload section of the shared session-logger
// corpus (packages/provide-uterm-ts/testdata/gen_session_logger_golden.py,
// recorded from CPython; this copy is twinned byte-for-byte with the TS and
// C# ones by scripts/check_protocol_drift.py).
type payloadGolden struct {
	Payloads struct {
		Patterns []string `json:"patterns"`
		Calls    []struct {
			Method    string         `json:"method"`
			Name      string         `json:"name"`
			Direction string         `json:"direction"`
			Data      map[string]any `json:"data"`
		} `json:"calls"`
		Redacted []any `json:"redacted"`
		Verbatim []any `json:"verbatim"`
	} `json:"payloads"`
}

func loadPayloadGolden(t *testing.T) payloadGolden {
	t.Helper()
	raw, err := os.ReadFile("testdata/session_logger_golden.json")
	if err != nil {
		t.Fatal(err)
	}
	var g payloadGolden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	return g
}

// replayPayloads drives a fresh wire-mode logger through the corpus calls
// and returns its entries normalised the way the generator recorded them.
func replayPayloads(t *testing.T, g payloadGolden, redactor redaction.Redactor) []any {
	t.Helper()
	store := recording.NewInMemoryStore()
	l := New(store, Options{ControlChannelMode: ModeWire, Redactor: redactor, FlushInterval: time.Hour})
	if err := l.Start("s1"); err != nil {
		t.Fatal(err)
	}
	for _, call := range g.Payloads.Calls {
		var err error
		if call.Method == "event" {
			err = l.LogEvent(call.Name, call.Data)
		} else {
			err = l.LogControl(call.Direction, call.Data)
		}
		if err != nil {
			t.Fatal(err)
		}
	}
	if err := l.Stop(); err != nil {
		t.Fatal(err)
	}
	got, err := store.GetEntries("s1", recording.Query{Limit: 500})
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var out []any
	if err := json.Unmarshal(encoded, &out); err != nil {
		t.Fatal(err)
	}
	for _, item := range out {
		entry := item.(map[string]any)
		delete(entry, "ts")
		if entry["event"] == "log_start" {
			entry["data"] = map[string]any{"stripped": true}
		}
	}
	return out
}

func TestPayloadParityWithCPythonRedacted(t *testing.T) {
	g := loadPayloadGolden(t)
	redactor, err := redaction.MakeRedactor(g.Payloads.Patterns)
	if err != nil {
		t.Fatal(err)
	}
	got := replayPayloads(t, g, redactor)
	if !reflect.DeepEqual(got, g.Payloads.Redacted) {
		gotJSON, _ := json.Marshal(got)
		wantJSON, _ := json.Marshal(g.Payloads.Redacted)
		t.Fatalf("redacted payloads diverge from CPython\n got: %s\nwant: %s", gotJSON, wantJSON)
	}
}

func TestPayloadParityWithCPythonVerbatim(t *testing.T) {
	g := loadPayloadGolden(t)
	got := replayPayloads(t, g, nil)
	if !reflect.DeepEqual(got, g.Payloads.Verbatim) {
		gotJSON, _ := json.Marshal(got)
		wantJSON, _ := json.Marshal(g.Payloads.Verbatim)
		t.Fatalf("verbatim payloads diverge from CPython\n got: %s\nwant: %s", gotJSON, wantJSON)
	}
}

func TestLogControlMasksResumeToken(t *testing.T) {
	store := recording.NewInMemoryStore()
	redactor, _ := redaction.MakeRedactor([]string{`secret\w*`})
	l := newLogger(t, store, Options{ControlChannelMode: ModeWire, Redactor: redactor})
	if err := l.LogControl("recv", map[string]any{"type": "resume", "token": "rt_1", "player_id": 2}); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	ctrl := entries(t, store, "control_recv")[0]["data"].(map[string]any)["control"].(map[string]any)
	if ctrl["token"] != redaction.SensitiveKeyMarker || ctrl["player_id"] != 2 {
		t.Fatalf("control = %v", ctrl)
	}
}

func TestDeepFrameIsRecordedWithoutUnboundedRecursion(t *testing.T) {
	var deep any = "secretDeep"
	for i := 0; i < 5000; i++ {
		deep = map[string]any{"a": deep}
	}
	store := recording.NewInMemoryStore()
	redactor, _ := redaction.MakeRedactor([]string{`secret\w*`})
	l := newLogger(t, store, Options{ControlChannelMode: ModeWire, Redactor: redactor})
	if err := l.LogControl("recv", map[string]any{"type": "x", "payload": deep}); err != nil {
		t.Fatal(err)
	}
	if err := l.LogEvent("deep", map[string]any{"payload": deep}); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	all, _ := store.GetEntries("sess-1", recording.Query{Limit: 500})
	encoded, err := json.Marshal(all)
	if err != nil {
		t.Fatal(err)
	}
	text := string(encoded)
	if strings.Contains(text, "secretDeep") || strings.Count(text, redaction.DepthLimitMarker) != 2 {
		t.Fatalf("deep frame not capped: %d markers", strings.Count(text, redaction.DepthLimitMarker))
	}
}

func TestPayloadIsCopiedWithoutRedactor(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{ControlChannelMode: ModeWire})
	data := map[string]any{"description": "secretValue", "token": "abc"}
	frame := map[string]any{"type": "paste", "text": "secretPaste"}
	if err := l.LogEvent("annotation", data); err != nil {
		t.Fatal(err)
	}
	if err := l.LogControl("send", frame); err != nil {
		t.Fatal(err)
	}
	data["description"] = "changed"
	frame["text"] = "changed"
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	ann := entries(t, store, "annotation")[0]["data"].(map[string]any)
	if ann["description"] != "secretValue" || ann["token"] != "abc" {
		t.Fatalf("annotation = %v", ann)
	}
	ctrl := entries(t, store, "control_send")[0]["data"].(map[string]any)["control"].(map[string]any)
	if ctrl["text"] != "secretPaste" {
		t.Fatalf("control = %v", ctrl)
	}
}

func TestPayloadIsDeepCopiedWithoutRedactor(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{ControlChannelMode: ModeWire})
	cursor := map[string]any{"x": 1}
	notes := []any{"n"}
	inner := map[string]any{"k": []any{1}}
	if err := l.LogEvent("annotation", map[string]any{"cursor": cursor, "notes": notes}); err != nil {
		t.Fatal(err)
	}
	if err := l.LogControl("send", map[string]any{"inner": inner}); err != nil {
		t.Fatal(err)
	}
	cursor["x"] = 99
	notes[0] = "changed"
	inner["k"] = "changed"
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	ann := entries(t, store, "annotation")[0]["data"].(map[string]any)
	if ann["cursor"].(map[string]any)["x"] != 1 || ann["notes"].([]any)[0] != "n" {
		t.Fatalf("annotation aliased the caller's containers: %v", ann)
	}
	ctrl := entries(t, store, "control_send")[0]["data"].(map[string]any)["control"].(map[string]any)
	if _, ok := ctrl["inner"].(map[string]any)["k"].([]any); !ok {
		t.Fatalf("control aliased the caller's containers: %v", ctrl)
	}
}

func TestNilPayloadWithRedactorRecordsEmptyObject(t *testing.T) {
	store := recording.NewInMemoryStore()
	redactor, _ := redaction.MakeRedactor([]string{`x`})
	l := newLogger(t, store, Options{Redactor: redactor})
	if err := l.LogEvent("empty", nil); err != nil {
		t.Fatal(err)
	}
	if err := l.Flush(); err != nil {
		t.Fatal(err)
	}
	if got := entries(t, store, "empty")[0]["data"].(map[string]any); len(got) != 0 {
		t.Fatalf("data = %v", got)
	}
}

func TestNothingIsRedactedPastTheQuota(t *testing.T) {
	var seen []string
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{
		MaxBytes:           1,
		ControlChannelMode: ModeWire,
		Redactor: func(s string) string {
			seen = append(seen, s)
			return s
		},
	})
	_ = l.LogEvent("dropped", map[string]any{"d": "dropped"})
	_ = l.LogControl("send", map[string]any{"text": "dropped"})
	_ = l.LogWire("send", "dropped")
	_ = l.LogSend("dropped")
	_ = l.LogScreen(session.Snapshot{Screen: "dropped"}, []byte("dropped"))
	if len(seen) != 0 {
		t.Fatalf("redactor ran past the quota: %v", seen)
	}
	if got := entries(t, store, ""); len(got) != 1 { // log_start only (Stop runs at cleanup)
		t.Fatalf("entries = %v", got)
	}
}

// A caller can pass the entry check and still find the quota spent once it
// holds the lock (another goroutine wrote in between); writeEvent re-checks.
func TestWriteEventRechecksQuotaUnderLock(t *testing.T) {
	store := recording.NewInMemoryStore()
	l := newLogger(t, store, Options{MaxBytes: 1 << 20})
	l.bytesWritten.Store(int64(l.maxBytes))
	if err := l.writeEvent("late", map[string]any{"a": 1}); err != nil {
		t.Fatal(err)
	}
	if got := entries(t, store, "late"); len(got) != 0 {
		t.Fatalf("late write recorded: %v", got)
	}
}
