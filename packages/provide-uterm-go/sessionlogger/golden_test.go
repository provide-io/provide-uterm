//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sessionlogger

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/recording"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/redaction"
)

// The recording format, held to the Python reference.
//
// testdata/session_logger_golden.json is what CPython's SessionLogger writes
// for a fixed script (regenerate with testdata/gen_session_logger_golden.py
// from the repository root), with only the fresh-by-design timestamps
// stripped. The TypeScript and C# ports are held to the same corpus. A Go
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
