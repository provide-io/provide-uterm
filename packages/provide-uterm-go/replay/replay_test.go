//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package replay

import (
	"encoding/base64"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeLog(t *testing.T, lines ...string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "log.jsonl")
	if err := os.WriteFile(path, []byte(strings.Join(lines, "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

func TestRebuildRawStream(t *testing.T) {
	log := writeLog(t,
		`{"event":"log_start","data":{}}`,
		``,
		`{"event":"read","data":{"raw_bytes_b64":"`+b64("hello ")+`"}}`,
		`{"event":"send","data":{"raw_bytes_b64":"`+b64("IGNORED")+`"}}`,
		`{"event":"read","data":{"raw_bytes_b64":"`+b64("world")+`"}}`,
		`{"event":"read","data":{}}`,
	)
	out := filepath.Join(t.TempDir(), "raw.bin")
	if err := RebuildRawStream(log, out); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(out)
	if err != nil || string(got) != "hello world" {
		t.Fatalf("got %q err %v", got, err)
	}
}

func TestRebuildRawStreamErrors(t *testing.T) {
	if err := RebuildRawStream(filepath.Join(t.TempDir(), "missing"), "/dev/null"); err == nil {
		t.Fatal("expected read error")
	}
	badJSON := writeLog(t, `{not json}`)
	if err := RebuildRawStream(badJSON, filepath.Join(t.TempDir(), "o")); err == nil {
		t.Fatal("expected json error")
	}
	badB64 := writeLog(t, `{"event":"read","data":{"raw_bytes_b64":"!!!"}}`)
	if err := RebuildRawStream(badB64, filepath.Join(t.TempDir(), "o")); err == nil {
		t.Fatal("expected base64 error")
	}
	good := writeLog(t, `{"event":"read","data":{"raw_bytes_b64":"`+b64("x")+`"}}`)
	if err := RebuildRawStream(good, filepath.Join(t.TempDir(), "no", "dir", "o")); err == nil {
		t.Fatal("expected write error")
	}
}

func TestReplayLogRendersFramesWithTiming(t *testing.T) {
	log := writeLog(t,
		`{"event":"read","ts":10.0,"data":{"screen":"frame one"}}`,
		`   `,
		`{"event":"send","ts":10.5,"data":{"keys":"x"}}`,
		`not json at all`,
		`{"event":"read","ts":12.0,"data":{"screen":"frame two"}}`,
		`{"event":"read","ts":13.0,"data":{}}`,
	)
	var out strings.Builder
	var slept []time.Duration
	var warned strings.Builder
	logger := slog.New(slog.NewTextHandler(&warned, nil))
	err := ReplayLog(log, ReplayOptions{
		Speed:  2.0,
		Output: &out,
		Logger: logger,
		Sleep:  func(d time.Duration) { slept = append(slept, d) },
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(out.String(), "\x1b[2J\x1b[H") != 2 {
		t.Fatalf("out = %q", out.String())
	}
	if !strings.Contains(out.String(), "frame one") || !strings.Contains(out.String(), "frame two") {
		t.Fatalf("out = %q", out.String())
	}
	// (12.0 - 10.0) / 2.0 = 1s between frames.
	if len(slept) != 1 || slept[0] != time.Second {
		t.Fatalf("slept = %v", slept)
	}
	if !strings.Contains(warned.String(), "replay_log corrupt line skipped") {
		t.Fatalf("warned = %q", warned.String())
	}
}

func TestReplayLogStepMode(t *testing.T) {
	log := writeLog(t,
		`{"event":"read","ts":1.0,"data":{"screen":"a"}}`,
		`{"event":"read","ts":9.0,"data":{"screen":"b"}}`,
	)
	var out strings.Builder
	err := ReplayLog(log, ReplayOptions{
		Step:   true,
		Output: &out,
		Input:  strings.NewReader("\n\n"),
		Sleep:  func(time.Duration) { t.Fatal("step mode must not sleep") },
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(out.String(), "-- next --") != 2 {
		t.Fatalf("out = %q", out.String())
	}
	// Input exhausted (EOF) is tolerated.
	err = ReplayLog(log, ReplayOptions{Step: true, Output: &out, Input: strings.NewReader("")})
	if err != nil {
		t.Fatal(err)
	}
}

func TestReplayLogCustomEventsAndSpeedClamp(t *testing.T) {
	log := writeLog(t,
		`{"event":"screen","ts":1.0,"data":{"screen":"s1"}}`,
		`{"event":"custom","ts":2.0,"data":{"screen":"c1"}}`,
		`{"event":"custom","ts":3.0,"data":{"screen":"c2"}}`,
	)
	var out strings.Builder
	var slept []time.Duration
	err := ReplayLog(log, ReplayOptions{
		Events: []string{"custom"},
		// Speed above the clamp behaves as 100x.
		Speed:  1e6,
		Output: &out,
		Sleep:  func(d time.Duration) { slept = append(slept, d) },
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(out.String(), "s1") || !strings.Contains(out.String(), "c2") {
		t.Fatalf("out = %q", out.String())
	}
	if len(slept) != 1 || slept[0] != 10*time.Millisecond {
		t.Fatalf("slept = %v", slept)
	}
}

func TestReplayLogMissingFile(t *testing.T) {
	if err := ReplayLog(filepath.Join(t.TempDir(), "missing"), ReplayOptions{}); err == nil {
		t.Fatal("expected error")
	}
}

func TestReplayLogWriteError(t *testing.T) {
	log := writeLog(t, `{"event":"read","ts":1.0,"data":{"screen":"x"}}`)
	if err := ReplayLog(log, ReplayOptions{Output: failWriter{}}); err == nil {
		t.Fatal("expected write error")
	}
	// Step-mode prompt write error.
	if err := ReplayLog(log, ReplayOptions{Step: true, Output: &failAfter{n: 1}, Input: strings.NewReader("\n")}); err == nil {
		t.Fatal("expected step write error")
	}
}

func TestReplayLogStepInputError(t *testing.T) {
	log := writeLog(t, `{"event":"read","ts":1.0,"data":{"screen":"x"}}`)
	var out strings.Builder
	err := ReplayLog(log, ReplayOptions{Step: true, Output: &out, Input: errReader{}})
	if err == nil {
		t.Fatal("expected input error")
	}
}

type errReader struct{}

func (errReader) Read([]byte) (int, error) { return 0, os.ErrClosed }

type failWriter struct{}

func (failWriter) Write([]byte) (int, error) { return 0, os.ErrClosed }

type failAfter struct{ n int }

func (f *failAfter) Write(p []byte) (int, error) {
	if f.n <= 0 {
		return 0, os.ErrClosed
	}
	f.n--
	return len(p), nil
}
