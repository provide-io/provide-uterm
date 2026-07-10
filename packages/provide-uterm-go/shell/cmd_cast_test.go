//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package shell

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func makeCastText(version int, events [][2]any) string {
	header, _ := json.Marshal(map[string]any{"version": version, "width": 80, "height": 24})
	lines := []string{string(header)}
	for _, e := range events {
		row, _ := json.Marshal([]any{e[0], "o", e[1]})
		lines = append(lines, string(row))
	}
	return strings.Join(lines, "\n")
}

// serveText starts a server returning body, wired to a dispatcher.
func serveText(t *testing.T, body string) (*CommandDispatcher, string) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	d := newDispatcher(nil)
	d.client = srv.Client()
	return d, srv.URL
}

func writeTemp(t *testing.T, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "demo.cast")
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func dispatchResult(t *testing.T, d *CommandDispatcher, line string) Result {
	t.Helper()
	return d.Dispatch(context.Background(), line)
}

func TestCastNoArgs(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast"); !strings.Contains(got, "usage:") {
		t.Fatalf("= %q", got)
	}
}

func TestCastInHelp(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "help"); !strings.Contains(got, "cast") {
		t.Fatalf("= %q", got)
	}
	if got := dispatchText(t, d, "help cast"); !strings.Contains(got, "--fps") {
		t.Fatalf("= %q", got)
	}
}

func TestCastUnknownFlag(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast --bogus https://example.com/demo.cast"); !strings.Contains(got, "unknown flag") {
		t.Fatalf("= %q", got)
	}
}

func TestCastBadURLScheme(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast ftp://example.com/demo.cast"); !strings.Contains(got, "unsupported URL scheme") {
		t.Fatalf("= %q", got)
	}
}

func TestCastFileNotFound(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast file:///nonexistent/path/demo.cast"); !strings.Contains(got, "file not found") {
		t.Fatalf("= %q", got)
	}
}

func TestCastFileURLSuccess(t *testing.T) {
	p := writeTemp(t, makeCastText(2, [][2]any{{0.0, "hello "}, {0.5, "world\r\n"}}))
	d := newDispatcher(nil)
	r := dispatchResult(t, d, "cast file://"+p)
	if r.Animated == nil || len(r.Animated.Frames) <= 1 || r.Animated.Loop {
		t.Fatalf("cast file = %+v", r)
	}
}

func TestCastHTTPSuccess(t *testing.T) {
	d, url := serveText(t, makeCastText(2, [][2]any{{0.0, "hi\r\n"}, {1.0, "bye\r\n"}}))
	r := dispatchResult(t, d, "cast "+url)
	if r.Animated == nil || len(r.Animated.Frames) <= 1 {
		t.Fatalf("cast http = %+v", r)
	}
}

func TestCastNetworkError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	url := srv.URL
	client := srv.Client()
	srv.Close()
	d := newDispatcher(nil)
	d.client = client
	if got := dispatchText(t, d, "cast "+url); !strings.Contains(got, "cannot fetch") {
		t.Fatalf("= %q", got)
	}
}

func TestCastEmptyFile(t *testing.T) {
	d, url := serveText(t, "")
	if got := dispatchText(t, d, "cast "+url); !strings.Contains(got, "empty cast file") {
		t.Fatalf("= %q", got)
	}
}

func TestCastInvalidHeaderJSON(t *testing.T) {
	d, url := serveText(t, "NOT JSON\n[0.0,\"o\",\"hi\"]")
	if got := dispatchText(t, d, "cast "+url); !strings.Contains(got, "invalid cast header") {
		t.Fatalf("= %q", got)
	}
}

func TestCastHeaderNotObject(t *testing.T) {
	d, url := serveText(t, "[1, 2, 3]\n[0.0,\"o\",\"hi\"]")
	if got := dispatchText(t, d, "cast "+url); !strings.Contains(got, "invalid cast header") {
		t.Fatalf("= %q", got)
	}
}

func TestCastWrongVersion(t *testing.T) {
	d, url := serveText(t, makeCastText(1, nil))
	if got := dispatchText(t, d, "cast "+url); !strings.Contains(got, "unsupported asciicast version") {
		t.Fatalf("= %q", got)
	}
}

func TestCastNoOutputEvents(t *testing.T) {
	// Header + a non-"o" (stdin) event only.
	header, _ := json.Marshal(map[string]any{"version": 2})
	row, _ := json.Marshal([]any{0.5, "i", "keystroke"})
	d, url := serveText(t, string(header)+"\n"+string(row)+"\n")
	if got := dispatchText(t, d, "cast "+url); !strings.Contains(got, "no output events") {
		t.Fatalf("= %q", got)
	}
}

func TestCastSkipsMalformedEventLines(t *testing.T) {
	header, _ := json.Marshal(map[string]any{"version": 2})
	good1, _ := json.Marshal([]any{0.0, "o", "good output\r\n"})
	good2, _ := json.Marshal([]any{1.0, "o", "more output\r\n"})
	text := strings.Join([]string{string(header), "BROKEN", string(good1), `{"not": "a list"}`, string(good2)}, "\n")
	p := writeTemp(t, text)
	d := newDispatcher(nil)
	r := dispatchResult(t, d, "cast file://"+p)
	if r.Animated == nil || !anyFrameContains(r.Animated.Frames, "good output") {
		t.Fatalf("= %+v", r)
	}
}

func TestCastEmptyLeadingBuckets(t *testing.T) {
	p := writeTemp(t, makeCastText(2, [][2]any{{2.0, "delayed output\r\n"}}))
	d := newDispatcher(nil)
	r := dispatchResult(t, d, "cast file://"+p)
	if r.Animated == nil || !anyFrameContains(r.Animated.Frames, "delayed output") {
		t.Fatalf("= %+v", r)
	}
}

func TestCastAllEmptyData(t *testing.T) {
	p := writeTemp(t, makeCastText(2, [][2]any{{0.0, ""}, {0.1, ""}}))
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast file://"+p); !strings.Contains(got, "no displayable output") {
		t.Fatalf("= %q", got)
	}
}

func TestCastLoopFlag(t *testing.T) {
	p := writeTemp(t, makeCastText(2, [][2]any{{0.0, "hello\r\n"}, {0.5, "world\r\n"}}))
	d := newDispatcher(nil)
	r := dispatchResult(t, d, "cast --loop file://"+p)
	if r.Animated == nil || !r.Animated.Loop {
		t.Fatalf("= %+v", r)
	}
}

func TestCastFPSFlag(t *testing.T) {
	p := writeTemp(t, makeCastText(2, [][2]any{{0.0, "hello\r\n"}, {2.0, "world\r\n"}}))
	d := newDispatcher(nil)
	r := dispatchResult(t, d, "cast --fps 5 file://"+p)
	if r.Animated == nil || r.Animated.FPS != 5.0 {
		t.Fatalf("= %+v", r)
	}
}

func TestCastBadFPSValue(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast --fps abc file:///x"); !strings.Contains(got, "invalid --fps") {
		t.Fatalf("= %q", got)
	}
}

func TestCastNonNumericTimestamp(t *testing.T) {
	// A non-numeric timestamp event is skipped (toFloat fails).
	d, url := serveText(t, `{"version":2}`+"\n"+`["notanum","o","data\r\n"]`)
	if got := dispatchText(t, d, "cast "+url); !strings.Contains(got, "no output events") {
		t.Fatalf("= %q", got)
	}
}

func TestCastNegativeTimestamp(t *testing.T) {
	// A negative timestamp exercises the nFrames<1 and idx<0 clamps.
	d, url := serveText(t, `{"version":2}`+"\n"+`[-1.0,"o","neg\r\n"]`)
	r := dispatchResult(t, d, "cast "+url)
	if r.Animated == nil || !anyFrameContains(r.Animated.Frames, "neg") {
		t.Fatalf("= %+v", r)
	}
}

func TestCastUnsortedTimestamps(t *testing.T) {
	// A later line with a smaller timestamp makes an earlier event's index
	// exceed nFrames-1, exercising the high-index clamp.
	d, url := serveText(t, `{"version":2}`+"\n"+`[5.0,"o","big\r\n"]`+"\n"+`[0.0,"o","small\r\n"]`)
	r := dispatchResult(t, d, "cast "+url)
	if r.Animated == nil || !anyFrameContains(r.Animated.Frames, "big") {
		t.Fatalf("= %+v", r)
	}
}

func TestCastMalformedHTTPURL(t *testing.T) {
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast http://%zz/x.cast"); !strings.Contains(got, "cannot fetch") {
		t.Fatalf("= %q", got)
	}
}

func TestCastFileReadError(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root: permission checks do not apply")
	}
	p := writeTemp(t, makeCastText(2, [][2]any{{0.0, "hi\r\n"}}))
	if err := os.Chmod(p, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(p, 0o600) })
	// Confirm the file is actually unreadable in this environment.
	if _, err := os.ReadFile(p); err == nil {
		t.Skip("file still readable (permissive filesystem)")
	}
	d := newDispatcher(nil)
	if got := dispatchText(t, d, "cast file://"+p); !strings.Contains(got, "cannot fetch") {
		t.Fatalf("= %q", got)
	}
}

func anyFrameContains(frames []string, sub string) bool {
	for _, f := range frames {
		if strings.Contains(f, sub) {
			return true
		}
	}
	return false
}
