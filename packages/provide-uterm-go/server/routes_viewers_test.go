//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// closeWatcher is a browser that records its frames and, when the server
// closes it, the close code and reason.
type closeWatcher struct {
	conn   *websocket.Conn
	frames chan map[string]any
	closed chan websocket.CloseError
}

func dialWatcher(t *testing.T, ctx context.Context, url string, headers http.Header) *closeWatcher {
	t.Helper()
	conn, _, err := websocket.Dial(ctx, url, &websocket.DialOptions{HTTPHeader: headers})
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	conn.SetReadLimit(1 << 20)
	w := &closeWatcher{conn: conn, frames: make(chan map[string]any, 64), closed: make(chan websocket.CloseError, 1)}
	go func() {
		dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
		for {
			_, raw, err := conn.Read(ctx)
			if err != nil {
				var ce websocket.CloseError
				if !errors.As(err, &ce) {
					ce = websocket.CloseError{Code: -1, Reason: err.Error()}
				}
				w.closed <- ce
				return
			}
			events, _ := dec.Feed(string(raw))
			for _, ev := range events {
				if c, ok := ev.(controlchannel.ControlChunk); ok {
					w.frames <- c.Control
				}
			}
		}
	}()
	return w
}

func (w *closeWatcher) waitFrame(t *testing.T, typ string) map[string]any {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		select {
		case f := <-w.frames:
			if f["type"] == typ {
				return f
			}
		case ce := <-w.closed:
			t.Fatalf("closed (%d %q) while waiting for %q", ce.Code, ce.Reason, typ)
		case <-deadline:
			t.Fatalf("timed out waiting for %q", typ)
		}
	}
}

func (w *closeWatcher) waitClose(t *testing.T) websocket.CloseError {
	t.Helper()
	select {
	case ce := <-w.closed:
		return ce
	case <-time.After(5 * time.Second):
		t.Fatal("socket was not closed")
		return websocket.CloseError{}
	}
}

func headers(subject, role string, iat float64) http.Header {
	return http.Header{"X-Subject": {subject}, "X-Role": {role}, "X-Iat": {strconv.FormatFloat(iat, 'f', -1, 64)}}
}

func flat(h http.Header) map[string]string {
	out := map[string]string{}
	for k, v := range h {
		out[k] = v[0]
	}
	return out
}

func TestRevokeViewersClosesShareWatchersAndRefusesTheirTokens(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	url := base + "/ws/browser/bb/term"
	watcherHeaders := headers("share:bb:blackbetty:7", "viewer", 100)

	watcher := dialWatcher(t, ctx, url, watcherHeaders)
	admin := dialWatcher(t, ctx, url, headers("admin1", "admin", 100))
	plain := dialWatcher(t, ctx, url, headers("alice", "viewer", 100))
	defer func() {
		for _, w := range []*closeWatcher{watcher, admin, plain} {
			_ = w.conn.Close(websocket.StatusNormalClosure, "")
		}
	}()
	if role := watcher.waitFrame(t, "hello")["role"]; role != "viewer" {
		t.Fatalf("watcher role %v", role)
	}
	admin.waitFrame(t, "hello")
	plain.waitFrame(t, "hello")

	path := "/api/sessions/bb/viewers/revoke"
	if rec := ts.do("POST", path, "", flat(watcherHeaders)); rec.Code != http.StatusForbidden {
		t.Fatalf("share viewer revoking: %d", rec.Code)
	}
	if rec := ts.do("POST", path, "", flat(headers("alice", "viewer", 100))); rec.Code != http.StatusForbidden {
		t.Fatalf("plain viewer revoking: %d", rec.Code)
	}
	rec := ts.do("POST", path, "", flat(headers("owner1", "operator", 100)))
	if rec.Code != http.StatusOK {
		t.Fatalf("owner revoking: %d %s", rec.Code, rec.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["session_id"] != "bb" || body["closed"] != 1.0 || body["close_code"] != 4410.0 ||
		body["close_reason"] != "viewer access revoked" {
		t.Fatalf("body %v", body)
	}
	revokedAt, ok := body["revoked_at"].(float64)
	if !ok || revokedAt <= 100 {
		t.Fatalf("revoked_at %v", body["revoked_at"])
	}

	ce := watcher.waitClose(t)
	if ce.Code != 4410 || ce.Reason != "viewer access revoked" {
		t.Fatalf("watcher closed with %d %q", ce.Code, ce.Reason)
	}

	// The admin and the plain viewer keep their sockets.
	for _, w := range []*closeWatcher{admin, plain} {
		payload, _ := controlchannel.EncodeControlFrame(map[string]any{"type": "ping"})
		if err := w.conn.Write(ctx, websocket.MessageText, []byte(payload)); err != nil {
			t.Fatalf("write: %v", err)
		}
		w.waitFrame(t, "pong")
	}

	// The revoked token can neither reconnect nor read.
	if conn, resp, err := websocket.Dial(ctx, url, &websocket.DialOptions{HTTPHeader: watcherHeaders}); err == nil {
		_ = conn.Close(websocket.StatusNormalClosure, "")
		t.Fatal("revoked token reconnected")
	} else if resp == nil || resp.StatusCode != http.StatusForbidden {
		t.Fatalf("revoked reconnect: resp=%v err=%v", resp, err)
	}
	if rec := ts.do("GET", "/api/sessions/bb", "", flat(watcherHeaders)); rec.Code != http.StatusForbidden {
		t.Fatalf("revoked token read: %d", rec.Code)
	}

	// A token issued after the cutoff watches again.
	fresh := dialWatcher(t, ctx, url, headers("share:bb:blackbetty:7", "viewer", revokedAt+1))
	defer func() { _ = fresh.conn.Close(websocket.StatusNormalClosure, "") }()
	fresh.waitFrame(t, "hello")
}

func TestRevokeViewersUnknownSessionIs404(t *testing.T) {
	ts := newTestServer(t, nil)
	if rec := ts.do("POST", "/api/sessions/nope/viewers/revoke", "", adminHeaders()); rec.Code != http.StatusNotFound {
		t.Fatalf("unknown session: %d", rec.Code)
	}
}

func TestRevokeViewersWithNoLiveSocketsClosesNothing(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("quiet", "owner1", "private")
	rec := ts.do("POST", "/api/sessions/quiet/viewers/revoke", "", adminHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("revoke: %d", rec.Code)
	}
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	if body["closed"] != 0.0 {
		t.Fatalf("closed %v", body["closed"])
	}
}
