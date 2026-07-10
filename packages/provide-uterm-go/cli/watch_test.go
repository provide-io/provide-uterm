//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// makeChannelFrame builds a DLE/STX control-framed message wrapping obj.
func makeChannelFrame(obj map[string]any) string {
	payload, _ := json.Marshal(obj)
	return fmt.Sprintf("\x10\x02%08x:%s", len(payload), payload)
}

func TestExtractTunnelID(t *testing.T) {
	cases := map[string]string{
		"abc123":                               "abc123",
		"https://host/app/inspect/tun-9?x=1":   "tun-9",
		"https://host/s/short":                 "short",
		"http://host/app/operator/op_1":        "op_1",
		"https://host/app/session/sess-2#frag": "sess-2",
		"https://host/unknown/path":            "https://host/unknown/path",
	}
	for in, want := range cases {
		if got := extractTunnelID(in); got != want {
			t.Errorf("extractTunnelID(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestResolveWatchServer(t *testing.T) {
	s, err := resolveWatchServer("https://host.example/app/inspect/t1", "")
	if err != nil || s != "https://host.example" {
		t.Fatalf("derive from url: %q err=%v", s, err)
	}
	if _, err := resolveWatchServer("bare-id", ""); err == nil {
		t.Error("bare id without --server should error")
	}
	if s, _ := resolveWatchServer("bare-id", "https://x"); s != "https://x" {
		t.Errorf("explicit server = %q", s)
	}
}

func TestWatchWSURL(t *testing.T) {
	got := watchWSURL("https://host.example/", "tun9")
	if got != "wss://host.example/ws/browser/tun9/term" {
		t.Errorf("wsURL = %q", got)
	}
	if got := watchWSURL("http://h:8080", "x"); got != "ws://h:8080/ws/browser/x/term" {
		t.Errorf("http→ws = %q", got)
	}
}

func TestReadWatchToken(t *testing.T) {
	if got := readWatchToken("tok", ""); got != "tok" {
		t.Errorf("explicit token = %q", got)
	}
	f := filepath.Join(t.TempDir(), "tok")
	_ = os.WriteFile(f, []byte("  filetok\n"), 0o600)
	if got := readWatchToken("", f); got != "filetok" {
		t.Errorf("file token = %q", got)
	}
	if got := readWatchToken("", filepath.Join(t.TempDir(), "nope")); got != "" {
		t.Errorf("missing file = %q", got)
	}
}

func TestParseHTTPFrames(t *testing.T) {
	req := makeChannelFrame(map[string]any{"_channel": "http", "type": "http_req", "id": "1", "method": "GET", "url": "/a"})
	// A non-http control frame must be ignored.
	other := makeChannelFrame(map[string]any{"_channel": "term", "type": "snapshot"})
	frames := parseHTTPFrames("noise" + req + "gap" + other)
	if len(frames) != 1 || frames[0]["type"] != "http_req" {
		t.Fatalf("frames = %v", frames)
	}
}

func TestParseHTTPFramesMalformedHeader(t *testing.T) {
	// DLE STX but a non-hex length → skipped, no panic.
	if got := parseHTTPFrames("\x10\x02zzzzzzzz:{}"); len(got) != 0 {
		t.Errorf("malformed header should yield no frames, got %v", got)
	}
}

func TestModelHandleReqRes(t *testing.T) {
	m := newWatchModel("tun", "horizontal")
	nm, _ := m.Update(httpFrameMsg{"type": "http_req", "id": "r1", "method": "GET", "url": "/x"})
	m = nm.(watchModel)
	if len(m.exchanges) != 1 || m.exchanges[0].method != "GET" {
		t.Fatalf("after req: %+v", m.exchanges)
	}
	nm, _ = m.Update(httpFrameMsg{"type": "http_res", "id": "r1", "status": 200.0, "duration_ms": 12.0, "body_size": 2048.0})
	m = nm.(watchModel)
	ex := m.exchanges[0]
	if ex.status == nil || *ex.status != 200 || ex.resBodySize != 2048 {
		t.Fatalf("after res: %+v", ex)
	}
	if statusLabel(ex) != "200" || sizeLabel(ex) != "2.0KB" || durationLabel(ex) != "12ms" {
		t.Errorf("labels: %s %s %s", statusLabel(ex), sizeLabel(ex), durationLabel(ex))
	}
}

func TestModelKeysLayoutFilterNav(t *testing.T) {
	m := newWatchModel("tun", "horizontal")
	for _, meth := range []string{"GET", "POST", "GET"} {
		nm, _ := m.Update(httpFrameMsg{"type": "http_req", "id": meth, "method": meth, "url": "/"})
		m = nm.(watchModel)
	}
	// cycle layout horizontal→vertical.
	nm, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("l")})
	m = nm.(watchModel)
	if m.layoutMode != "vertical" {
		t.Errorf("layout = %q", m.layoutMode)
	}
	// method filter → GET.
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("f")})
	m = nm.(watchModel)
	if m.methodFilter != "GET" || len(m.filtered()) != 2 {
		t.Errorf("filter = %q, filtered=%d", m.methodFilter, len(m.filtered()))
	}
	// down moves cursor within filtered rows.
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyDown})
	m = nm.(watchModel)
	if m.cursor != 1 {
		t.Errorf("cursor = %d", m.cursor)
	}
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyUp})
	m = nm.(watchModel)
	if m.cursor != 0 {
		t.Errorf("cursor after up = %d", m.cursor)
	}
}

func TestModelModalDetail(t *testing.T) {
	m := newWatchModel("tun", "modal")
	nm, _ := m.Update(httpFrameMsg{"type": "http_req", "id": "1", "method": "GET", "url": "/x"})
	m = nm.(watchModel)
	body := base64.StdEncoding.EncodeToString([]byte("hello-body"))
	nm, _ = m.Update(httpFrameMsg{"type": "http_res", "id": "1", "status": 500.0, "status_text": "ERR",
		"duration_ms": 5.0, "headers": map[string]any{"X-A": "1"}, "body_b64": body, "body_size": 10.0})
	m = nm.(watchModel)
	// enter opens the modal; the view then renders the detail.
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = nm.(watchModel)
	if !m.showDetail {
		t.Fatal("enter should open modal detail")
	}
	view := m.View()
	if !strings.Contains(view, "GET /x") || !strings.Contains(view, "500 ERR") || !strings.Contains(view, "hello-body") {
		t.Errorf("modal view missing detail:\n%s", view)
	}
	// esc closes it.
	nm, _ = m.Update(tea.KeyMsg{Type: tea.KeyEsc})
	m = nm.(watchModel)
	if m.showDetail {
		t.Error("esc should close modal")
	}
}

func TestModelConnState(t *testing.T) {
	m := newWatchModel("tun", "horizontal")
	nm, _ := m.Update(connStateMsg{connected: true})
	m = nm.(watchModel)
	if !m.connected || !strings.Contains(m.View(), "Connected") {
		t.Error("connected state not reflected")
	}
}

func TestModelQuit(t *testing.T) {
	m := newWatchModel("tun", "horizontal")
	_, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("q")})
	if cmd == nil {
		t.Fatal("q should return a quit command")
	}
}

func TestHumanSize(t *testing.T) {
	cases := map[int]string{0: "0B", 512: "512B", 2048: "2.0KB", 3 * 1024 * 1024: "3.0MB"}
	for in, want := range cases {
		if got := humanSize(in); got != want {
			t.Errorf("humanSize(%d) = %q, want %q", in, got, want)
		}
	}
}

func TestDecodeBody(t *testing.T) {
	if got := decodeBody(base64.StdEncoding.EncodeToString([]byte("hi")), false, false, 2); got != "hi" {
		t.Errorf("decode = %q", got)
	}
	if got := decodeBody("!!!notb64", false, false, 0); got != "(decode error)" {
		t.Errorf("bad b64 = %q", got)
	}
	if got := decodeBody("", true, false, 2048); got != "(truncated, 2.0KB)" {
		t.Errorf("truncated = %q", got)
	}
	if got := decodeBody("", false, true, 1024); got != "(binary, 1.0KB)" {
		t.Errorf("binary = %q", got)
	}
	if got := decodeBody("", false, false, 0); got != "" {
		t.Errorf("empty = %q", got)
	}
}
