//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"context"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// wsHub is an in-process fake hub. It accepts a WebSocket and hands the
// server-side connection to a per-connection handler so each test drives the
// exchange it needs.
type wsHub struct {
	srv     *httptest.Server
	handler func(ctx context.Context, conn *websocket.Conn)
}

func newWSHub(t *testing.T, handler func(ctx context.Context, conn *websocket.Conn)) *wsHub {
	t.Helper()
	h := &wsHub{handler: handler}
	h.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		conn.SetReadLimit(-1)
		ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
		defer cancel()
		h.handler(ctx, conn)
	}))
	t.Cleanup(h.srv.Close)
	return h
}

// wsURL rewrites the httptest http:// URL to ws:// with the given path so the
// role inference (/ws/worker/ vs other) can be exercised.
func (h *wsHub) wsURL(path string) string {
	return "ws" + strings.TrimPrefix(h.srv.URL, "http") + path
}

// -- pure-function ports --------------------------------------------------

func TestEncodeLogicalFrameDataChannel(t *testing.T) {
	for _, typ := range []string{"term", "input"} {
		got, err := encodeLogicalFrame(map[string]any{"type": typ, "data": "abc"})
		if err != nil {
			t.Fatal(err)
		}
		if got != controlchannel.EncodeTerminalData("abc") {
			t.Fatalf("%s: %q", typ, got)
		}
	}
}

func TestEncodeLogicalFrameControlChannel(t *testing.T) {
	payload := map[string]any{"type": "hello", "worker_online": true}
	got, err := encodeLogicalFrame(payload)
	if err != nil {
		t.Fatal(err)
	}
	want, _ := controlchannel.EncodeControlFrame(payload)
	if got != want {
		t.Fatalf("control frame: %q != %q", got, want)
	}
}

func TestEncodeLogicalFrameDataDefaultsAndCoercion(t *testing.T) {
	// Missing data → empty string; non-string data → fmt.Sprint.
	got, _ := encodeLogicalFrame(map[string]any{"type": "term"})
	if got != controlchannel.EncodeTerminalData("") {
		t.Fatalf("missing data: %q", got)
	}
	got, _ = encodeLogicalFrame(map[string]any{"type": "input", "data": 42})
	if got != controlchannel.EncodeTerminalData("42") {
		t.Fatalf("int data: %q", got)
	}
}

func TestInferRole(t *testing.T) {
	if inferRole("ws://h/ws/worker/abc") != RoleWorker {
		t.Fatal("worker url")
	}
	if inferRole("ws://h/ws/browser/abc") != RoleBrowser {
		t.Fatal("browser url")
	}
}

func TestLogicalFrameDecoderRoles(t *testing.T) {
	b := NewLogicalFrameDecoder(RoleBrowser)
	frames, err := b.Feed(controlchannel.EncodeTerminalData("hello"))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(frames, []map[string]any{{"type": "term", "data": "hello"}}) {
		t.Fatalf("browser data: %v", frames)
	}

	w := NewLogicalFrameDecoder(RoleWorker)
	frames, _ = w.Feed(controlchannel.EncodeTerminalData("hi"))
	if !reflect.DeepEqual(frames, []map[string]any{{"type": "input", "data": "hi"}}) {
		t.Fatalf("worker data: %v", frames)
	}

	ctrl, _ := controlchannel.EncodeControlFrame(map[string]any{"type": "ping"})
	frames, _ = b.Feed(ctrl)
	if len(frames) != 1 || frames[0]["type"] != "ping" {
		t.Fatalf("control preserved: %v", frames)
	}
}

func TestLogicalFrameDecoderFIFO(t *testing.T) {
	d := NewLogicalFrameDecoder(RoleBrowser)
	var combined strings.Builder
	for i := 0; i < 3; i++ {
		ctrl, _ := controlchannel.EncodeControlFrame(map[string]any{"type": "ping", "seq": i})
		combined.WriteString(ctrl)
		combined.WriteString(controlchannel.EncodeTerminalData("data"))
	}
	frames, err := d.Feed(combined.String())
	if err != nil {
		t.Fatal(err)
	}
	if len(frames) != 6 {
		t.Fatalf("frame count: %d", len(frames))
	}
	for i := 0; i < 3; i++ {
		if frames[2*i]["type"] != "ping" {
			t.Fatalf("frame %d not ping: %v", 2*i, frames[2*i])
		}
		if frames[2*i+1]["type"] != "term" || frames[2*i+1]["data"] != "data" {
			t.Fatalf("frame %d not term-data: %v", 2*i+1, frames[2*i+1])
		}
	}
}

func TestLogicalFrameDecoderFinish(t *testing.T) {
	// The Go controlchannel decoder flushes complete plain data on Feed (unlike
	// the Python decoder poked directly in its test), so Feed surfaces the data
	// frame immediately and a subsequent Finish on a drained buffer is a clean
	// no-op.
	d := NewLogicalFrameDecoder(RoleWorker)
	frames, err := d.Feed("partial")
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(frames, []map[string]any{{"type": "input", "data": "partial"}}) {
		t.Fatalf("feed data: %v", frames)
	}
	rest, err := d.Finish()
	if err != nil {
		t.Fatal(err)
	}
	if len(rest) != 0 {
		t.Fatalf("finish should be empty: %v", rest)
	}
}

func TestLogicalFrameDecoderFeedError(t *testing.T) {
	d := NewLogicalFrameDecoder(RoleBrowser)
	// DLE followed by a non-DLE/non-STX byte is an invalid control prefix.
	if _, err := d.Feed("\x10X"); err == nil {
		t.Fatal("expected protocol error")
	}
}

func TestLogicalFrameDecoderFinishError(t *testing.T) {
	d := NewLogicalFrameDecoder(RoleBrowser)
	// A lone DLE at end-of-stream is a truncated control frame on Finish.
	if _, err := d.Feed("data\x10"); err != nil {
		t.Fatal(err)
	}
	if _, err := d.Finish(); err == nil {
		t.Fatal("expected truncated-frame error on finish")
	}
}

// -- live client over the fake hub ---------------------------------------

func TestSendFrameFramingOverHub(t *testing.T) {
	var mu sync.Mutex
	var got []string
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		for i := 0; i < 2; i++ {
			typ, raw, err := conn.Read(ctx)
			if err != nil {
				return
			}
			if typ != websocket.MessageText {
				t.Errorf("server got non-text: %v", typ)
			}
			mu.Lock()
			got = append(got, string(raw))
			mu.Unlock()
		}
		_ = conn.Close(websocket.StatusNormalClosure, "")
	})

	c, err := Dial(ctx(), hub.wsURL("/ws/worker/x"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()

	// A data-typed frame routes through the terminal-data channel...
	if err := c.SendFrame(ctx(), map[string]any{"type": "input", "data": "abc"}); err != nil {
		t.Fatal(err)
	}
	// ...and a control frame routes through the DLE/STX control channel.
	if err := c.SendJSON(ctx(), map[string]any{"type": "control", "action": "pause"}); err != nil {
		t.Fatal(err)
	}

	// Give the server goroutine time to record both messages.
	deadline := time.Now().Add(2 * time.Second)
	for {
		mu.Lock()
		n := len(got)
		mu.Unlock()
		if n >= 2 || time.Now().After(deadline) {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(got) != 2 {
		t.Fatalf("server received %d messages", len(got))
	}
	if got[0] != controlchannel.EncodeTerminalData("abc") {
		t.Fatalf("data-frame not terminal-encoded: %q", got[0])
	}
	// Assert framing via the controlchannel decoder: the second message decodes
	// to exactly one control chunk carrying the payload.
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	chunks, err := dec.Feed(got[1])
	if err != nil {
		t.Fatal(err)
	}
	if len(chunks) != 1 {
		t.Fatalf("control decode chunks: %d", len(chunks))
	}
	cc, ok := chunks[0].(controlchannel.ControlChunk)
	if !ok || cc.Control["action"] != "pause" {
		t.Fatalf("control chunk: %#v", chunks[0])
	}
}

func TestRecvFrameDecodesInterleaved(t *testing.T) {
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		// One WebSocket message carrying a control frame immediately followed by
		// terminal data — RecvFrame must split them and deliver FIFO.
		ctrl, _ := controlchannel.EncodeControlFrame(map[string]any{"type": "hello", "worker_online": true})
		msg := ctrl + controlchannel.EncodeTerminalData("typed")
		_ = conn.Write(ctx, websocket.MessageText, []byte(msg))
		// Block until the client closes.
		_, _, _ = conn.Read(ctx)
	})

	c, err := Dial(ctx(), hub.wsURL("/ws/worker/x"), &DialOptions{Headers: http.Header{"X-Test": {"1"}}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()

	f1, err := c.RecvFrame(ctx())
	if err != nil {
		t.Fatal(err)
	}
	if f1["type"] != "hello" || f1["worker_online"] != true {
		t.Fatalf("frame 1: %v", f1)
	}
	f2, err := c.ReceiveJSON(ctx())
	if err != nil {
		t.Fatal(err)
	}
	// Worker role → data surfaces as an "input" frame.
	if f2["type"] != "input" || f2["data"] != "typed" {
		t.Fatalf("frame 2: %v", f2)
	}
}

func TestRecvFrameRejectsBinary(t *testing.T) {
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Write(ctx, websocket.MessageBinary, []byte("raw-bytes"))
		_, _, _ = conn.Read(ctx)
	})
	c, err := Dial(ctx(), hub.wsURL("/ws/browser/x"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()

	if _, err := c.RecvFrame(ctx()); err == nil || !strings.Contains(err.Error(), "expected text WebSocket payload") {
		t.Fatalf("binary reject: %v", err)
	}
}

func TestRecvFrameReadError(t *testing.T) {
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Close(websocket.StatusNormalClosure, "bye")
	})
	c, err := Dial(ctx(), hub.wsURL("/ws/browser/x"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()
	if _, err := c.RecvFrame(ctx()); err == nil {
		t.Fatal("expected read error after server close")
	}
}

func TestSendRejectsBareJSONObject(t *testing.T) {
	c := NewControlWSClient(nil, RoleWorker) // no I/O: rejection happens before Write
	err := c.Send(ctx(), `{"type":"control","action":"pause"}`)
	if err == nil || !strings.Contains(err.Error(), "bare JSON control strings") {
		t.Fatalf("bare JSON reject: %v", err)
	}
}

func TestSendPassesNonObjectStrings(t *testing.T) {
	var mu sync.Mutex
	var got []string
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		for i := 0; i < 2; i++ {
			_, raw, err := conn.Read(ctx)
			if err != nil {
				return
			}
			mu.Lock()
			got = append(got, string(raw))
			mu.Unlock()
		}
	})
	c, err := Dial(ctx(), hub.wsURL("/ws/browser/x"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()

	// Non-JSON string and a JSON array (non-object) both pass through verbatim.
	if err := c.Send(ctx(), "not-json-{{{{"); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(ctx(), "[1,2,3]"); err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(2 * time.Second)
	for {
		mu.Lock()
		n := len(got)
		mu.Unlock()
		if n >= 2 || time.Now().After(deadline) {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 2 || got[0] != "not-json-{{{{" || got[1] != "[1,2,3]" {
		t.Fatalf("pass-through: %v", got)
	}
}

func TestDialInferredRoleBrowser(t *testing.T) {
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Write(ctx, websocket.MessageText, []byte(controlchannel.EncodeTerminalData("x")))
		_, _, _ = conn.Read(ctx)
	})
	c, err := Dial(ctx(), hub.wsURL("/ws/browser/x"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()
	f, err := c.RecvFrame(ctx())
	if err != nil {
		t.Fatal(err)
	}
	if f["type"] != "term" {
		t.Fatalf("browser role data type: %v", f)
	}
}

func TestDialRoleOverride(t *testing.T) {
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		_ = conn.Write(ctx, websocket.MessageText, []byte(controlchannel.EncodeTerminalData("x")))
		_, _, _ = conn.Read(ctx)
	})
	// URL says worker but override forces browser.
	c, err := Dial(ctx(), hub.wsURL("/ws/worker/x"), &DialOptions{Role: RoleBrowser})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close(websocket.StatusNormalClosure, "") }()
	f, _ := c.RecvFrame(ctx())
	if f["type"] != "term" {
		t.Fatalf("override role: %v", f)
	}
}

func TestDialError(t *testing.T) {
	if _, err := Dial(ctx(), "ws://127.0.0.1:1/ws/worker/x", nil); err == nil {
		t.Fatal("expected dial error")
	}
}

func TestWrappedClientCloseIsNoop(t *testing.T) {
	// A wrapped (non-owned) connection: Close must be a no-op and not panic on
	// a nil-ish conn we never dial. Use a real conn to be safe.
	hub := newWSHub(t, func(ctx context.Context, conn *websocket.Conn) {
		_, _, _ = conn.Read(ctx)
	})
	dialed, err := Dial(ctx(), hub.wsURL("/ws/browser/x"), nil)
	if err != nil {
		t.Fatal(err)
	}
	wrapped := NewControlWSClient(dialed.conn, RoleBrowser)
	if err := wrapped.Close(websocket.StatusNormalClosure, ""); err != nil {
		t.Fatalf("wrapped close should be no-op: %v", err)
	}
	_ = dialed.Close(websocket.StatusNormalClosure, "")
}

func TestSendFrameEncodeError(t *testing.T) {
	// A payload that cannot be JSON-encoded (a channel) makes EncodeControlFrame
	// fail, which SendFrame propagates without touching the socket.
	c := NewControlWSClient(nil, RoleBrowser)
	err := c.SendFrame(ctx(), map[string]any{"type": "x", "bad": make(chan int)})
	if err == nil {
		t.Fatal("expected encode error")
	}
}
