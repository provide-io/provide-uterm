//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/tunnel"
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

// TestRevokeRaceSocketRegisteredAfterTheScanIsClosed: the role was resolved
// before the revoke, the socket registers after the revoke's scan. The check
// right after registration must close it.
func TestRevokeRaceSocketRegisteredAfterTheScanIsClosed(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	prev := browserPreRegisterHook
	browserPreRegisterHook = func(workerID string) {
		_, _ = ts.srv.deps.Authz.RevokeShareViewers(workerID, ts.srv.deps.Clock.Wall())
	}
	defer func() { browserPreRegisterHook = prev }()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	watcher := dialWatcher(t, ctx, base+"/ws/browser/bb/term", headers("share:bb:blackbetty:7", "viewer", 100))
	ce := watcher.waitClose(t)
	if ce.Code != 4410 || ce.Reason != "viewer access revoked" {
		t.Fatalf("closed with %d %q", ce.Code, ce.Reason)
	}
	// An admin registering after a revoke is not touched.
	admin := dialWatcher(t, ctx, base+"/ws/browser/bb/term", headers("admin1", "admin", 100))
	defer func() { _ = admin.conn.Close(websocket.StatusNormalClosure, "") }()
	admin.waitFrame(t, "hello")
}

func TestRevokeEndsALiveShareViewerEventStream(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	ts.srv.MarkReady()
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	open := func(subject, role string) *bufio.Reader {
		req, _ := http.NewRequestWithContext(ctx, "GET", httpSrv.URL+"/api/sessions/bb/events/stream", nil)
		for k, v := range headers(subject, role, 100) {
			req.Header[k] = v
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("open stream: %v %v", resp, err)
		}
		t.Cleanup(func() { _ = resp.Body.Close() })
		return bufio.NewReader(resp.Body)
	}
	watcher := open("share:bb:blackbetty:7", "viewer")
	plain := open("alice", "viewer")
	waitUntil(t, 5*time.Second, func() bool { return ts.srv.viewerStreams.count("bb") == 1 })

	rec := ts.do("POST", "/api/sessions/bb/viewers/revoke", "", adminHeaders())
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"closed":1`) {
		t.Fatalf("revoke: %d %s", rec.Code, rec.Body.String())
	}
	line, err := watcher.ReadString('\n')
	if err != nil || line != "data: {\"type\":\"viewer_access_revoked\",\"reason\":\"viewer access revoked\"}\n" {
		t.Fatalf("watcher got %q %v", line, err)
	}
	if _, err := watcher.ReadString('\n'); err != nil {
		t.Fatalf("blank separator: %v", err)
	}
	if _, err := watcher.ReadString('\n'); err == nil {
		t.Fatal("the stream was not ended")
	}
	if ts.srv.viewerStreams.count("bb") != 0 {
		t.Fatal("ended stream still registered")
	}
	// The plain viewer's stream is still open: the worker going away ends it.
	ts.hub.EventBus().CloseWorker("bb")
	if line, _ := plain.ReadString('\n'); !strings.Contains(line, "worker_disconnected") {
		t.Fatalf("plain viewer got %q", line)
	}
}

func TestShareViewerStreamOpenedAfterTheCutoffEndsAtOnce(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	prev := sseOpenHook
	sseOpenHook = func(id string) { _, _ = ts.srv.deps.Authz.RevokeShareViewers(id, ts.srv.deps.Clock.Wall()) }
	defer func() { sseOpenHook = prev }()
	rec := ts.do("GET", "/api/sessions/bb/events/stream", "", flat(headers("share:bb:blackbetty:7", "viewer", 100)))
	if rec.Body.String() != "data: {\"type\":\"viewer_access_revoked\",\"reason\":\"viewer access revoked\"}\n\n" {
		t.Fatalf("body %q", rec.Body.String())
	}
}

func TestRevokeRefusesOtherOwnersAndAdminsScopedElsewhere(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	path := "/api/sessions/bb/viewers/revoke"
	if rec := ts.do("POST", path, "", map[string]string{"X-Subject": "owner2", "X-Role": "operator"}); rec.Code != http.StatusForbidden {
		t.Fatalf("another session's owner: %d", rec.Code)
	}
	scoped := map[string]string{"X-Subject": "scoped", "X-Role": "admin", "X-Admin-Session-Scope": "elsewhere"}
	if rec := ts.do("POST", path, "", scoped); rec.Code != http.StatusForbidden {
		t.Fatalf("admin scoped to another session: %d", rec.Code)
	}
	scoped["X-Admin-Session-Scope"] = "bb"
	if rec := ts.do("POST", path, "", scoped); rec.Code != http.StatusOK {
		t.Fatalf("admin scoped to this session: %d", rec.Code)
	}
}

func TestRevokeLeavesTheOwnerAndTheHijackHolderConnected(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	worker, _, err := websocket.Dial(ctx, base+"/ws/worker/bb/term", nil)
	if err != nil {
		t.Fatalf("worker dial: %v", err)
	}
	defer func() { _ = worker.Close(websocket.StatusNormalClosure, "") }()
	go func() {
		for {
			if _, _, err := worker.Read(ctx); err != nil {
				return
			}
		}
	}()
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.Registry.Contains("bb") })

	holder := dialWatcher(t, ctx, base+"/ws/browser/bb/term", headers("admin1", "admin", 100))
	owner := dialWatcher(t, ctx, base+"/ws/browser/bb/term", headers("owner1", "operator", 100))
	defer func() {
		_ = holder.conn.Close(websocket.StatusNormalClosure, "")
		_ = owner.conn.Close(websocket.StatusNormalClosure, "")
	}()
	holder.waitFrame(t, "hello")
	if role := owner.waitFrame(t, "hello")["role"]; role != "operator" {
		t.Fatalf("owner role %v", role)
	}
	send := func(w *closeWatcher, msg map[string]any) {
		payload, _ := controlchannel.EncodeControlFrame(msg)
		if err := w.conn.Write(ctx, websocket.MessageText, []byte(payload)); err != nil {
			t.Fatalf("write: %v", err)
		}
	}
	send(holder, map[string]any{"type": "hijack_request"})
	for holder.waitFrame(t, "hijack_state")["owner"] != "me" {
	}
	rec := ts.do("POST", "/api/sessions/bb/viewers/revoke", "", flat(headers("owner1", "operator", 100)))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"closed":0`) {
		t.Fatalf("revoke: %d %s", rec.Code, rec.Body.String())
	}
	for _, w := range []*closeWatcher{holder, owner} {
		send(w, map[string]any{"type": "ping"})
		w.waitFrame(t, "pong")
	}
}

func TestDeletingASessionForgetsItsCutoff(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	if rec := ts.do("POST", "/api/sessions/bb/viewers/revoke", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("revoke %d", rec.Code)
	}
	if rec := ts.do("DELETE", "/api/sessions/bb", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("delete %d", rec.Code)
	}
	if _, ok := ts.srv.deps.Authz.ShareViewerRevocation("bb"); ok {
		t.Fatal("cutoff kept after delete")
	}
}

// TestTunnelViewersAreRevokedByRotationNotByTheViewerCutoff: revoke, rotate,
// and the new tunnel share link is admitted while the old one is not.
func TestTunnelViewersAreRevokedByRotationNotByTheViewerCutoff(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("tun", "op1", "private")
	ts.srv.deps.TunnelStore.PutToken("tun", tunnel.TokenRecord{
		ShareTokenHash: tunnel.HashToken("old-share"), TunnelType: "terminal", ExpiresAt: farFuture,
	})
	read := func(token string) int {
		return ts.do("GET", "/api/sessions/tun", "", map[string]string{"Cookie": "uterm_tunnel_tun=" + token}).Code
	}
	if code := read("old-share"); code != http.StatusOK {
		t.Fatalf("tunnel viewer before revoke: %d", code)
	}
	if rec := ts.do("POST", "/api/sessions/tun/viewers/revoke", "", operatorHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("revoke: %d %s", rec.Code, rec.Body.String())
	}
	if code := read("old-share"); code != http.StatusOK {
		t.Fatalf("tunnel viewer after a viewer revoke: %d (tunnel shares are out of scope)", code)
	}
	rec := ts.do("POST", "/api/tunnels/tun/tokens/rotate", "", operatorHeaders())
	if rec.Code != http.StatusOK {
		t.Fatalf("rotate: %d %s", rec.Code, rec.Body.String())
	}
	shareURL, _ := bodyMap(t, rec.Body.Bytes())["share_url"].(string)
	landed := ts.do("GET", shareURL[strings.Index(shareURL, "/s/"):], "", nil)
	cookie := landed.Header().Get("Set-Cookie")
	if landed.Code != http.StatusFound || !strings.HasPrefix(cookie, "uterm_tunnel_tun=") {
		t.Fatalf("invite: %d %q", landed.Code, cookie)
	}
	newToken := strings.TrimPrefix(strings.SplitN(cookie, ";", 2)[0], "uterm_tunnel_tun=")
	if code := read(newToken); code != http.StatusOK {
		t.Fatalf("new tunnel viewer after rotation: %d", code)
	}
	if code := read("old-share"); code != http.StatusUnauthorized {
		t.Fatalf("old tunnel viewer after rotation: %d", code)
	}
}

func TestAFullCutoffTableAnswers429(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	now := ts.srv.deps.Clock.Wall()
	for i := 0; i < serverauth.ShareViewerRevocationsMax; i++ {
		_, _ = ts.srv.deps.Authz.RevokeShareViewers(fmt.Sprintf("other-%d", i), now)
	}
	rec := ts.do("POST", "/api/sessions/bb/viewers/revoke", "", adminHeaders())
	if rec.Code != http.StatusTooManyRequests || !strings.Contains(rec.Body.String(), "too many sessions with revoked viewers") {
		t.Fatalf("full table: %d %s", rec.Code, rec.Body.String())
	}
}

func TestALongPollInFlightWhenItsViewerIsRevokedAnswers403(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	ts.reg.watchHook = func() { _, _ = ts.srv.deps.Authz.RevokeShareViewers("bb", ts.srv.deps.Clock.Wall()) }
	rec := ts.do("GET", "/api/sessions/bb/events/watch", "", flat(headers("share:bb:blackbetty:7", "viewer", 100)))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("revoked mid-poll: %d %s", rec.Code, rec.Body.String())
	}
	if rec := ts.do("GET", "/api/sessions/bb/events/watch", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("admin poll: %d", rec.Code)
	}
}

// TestARevokedSocketThatIgnoresTheCloseIsDroppedAfterTheGrace: a peer that
// never reads cannot answer the close handshake; the revoke stops waiting on it
// after the grace, and the connection is dropped once the handshake times out.
func TestARevokedSocketThatIgnoresTheCloseIsDroppedAfterTheGrace(t *testing.T) {
	prev := browserCloseGrace
	browserCloseGrace = 200 * time.Millisecond
	defer func() { browserCloseGrace = prev }()
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	base, closeFn := wsServer(t, ts)
	defer closeFn()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, base+"/ws/browser/bb/term", &websocket.DialOptions{
		HTTPHeader: headers("share:bb:blackbetty:7", "viewer", 100),
	})
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.CloseNow() }()
	waitUntil(t, 5*time.Second, func() bool { return ts.hub.BrowserCount(context.Background(), "bb") == 1 })
	started := time.Now()
	if rec := ts.do("POST", "/api/sessions/bb/viewers/revoke", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("revoke: %d", rec.Code)
	}
	if took := time.Since(started); took > 2*time.Second {
		t.Fatalf("revoke waited %v on a peer that ignores the close", took)
	}
	waitUntil(t, 9*time.Second, func() bool { return ts.hub.BrowserCount(context.Background(), "bb") == 0 })
}

func TestAnOwnersRevokesAreCappedPerPrincipalButAGlobalAdminsAreNot(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.reg.add("bb", "owner1", "public")
	ts.reg.add("bb2", "owner1", "public")
	now := ts.srv.deps.Clock.Wall()
	authz := ts.srv.deps.Authz
	for i := 0; i < serverauth.ShareViewerRevocationsPerPrincipalMax; i++ {
		_, _, _ = authz.RevokeShareViewersBy(fmt.Sprintf("mine-%d", i), now, "owner1")
	}
	if rec := ts.do("POST", "/api/sessions/bb/viewers/revoke", "", adminHeaders()); rec.Code != http.StatusOK {
		t.Fatalf("admin revoke: %d", rec.Code)
	}
	if _, ok := authz.ShareViewerRevocation("mine-0"); !ok {
		t.Fatal("a global admin's revoke counted against owner1")
	}
	if rec := ts.do("POST", "/api/sessions/bb2/viewers/revoke", "", flat(headers("owner1", "operator", 100))); rec.Code != http.StatusOK {
		t.Fatalf("owner revoke: %d", rec.Code)
	}
	if _, ok := authz.ShareViewerRevocation("mine-0"); ok {
		t.Error("owner1's revoke past its cap kept its oldest cutoff")
	}
	if _, ok := authz.ShareViewerRevocation("bb2"); !ok {
		t.Error("owner1's newest cutoff was not recorded")
	}
}

func TestACutoffTheCapEvictsIsAuditedBySessionAndPrincipal(t *testing.T) {
	var logs bytes.Buffer
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Logger = slog.New(slog.NewTextHandler(&logs, nil))
	})
	ts.reg.add("bb", "owner1", "public")
	now := ts.srv.deps.Clock.Wall()
	for i := 0; i < serverauth.ShareViewerRevocationsPerPrincipalMax; i++ {
		_, _, _ = ts.srv.deps.Authz.RevokeShareViewersBy(fmt.Sprintf("mine-%d", i), now, "owner1")
	}
	if rec := ts.do("POST", "/api/sessions/bb/viewers/revoke", "", flat(headers("owner1", "operator", 100))); rec.Code != http.StatusOK {
		t.Fatalf("owner revoke: %d", rec.Code)
	}
	var evicted string
	for _, line := range strings.Split(logs.String(), "\n") {
		if strings.Contains(line, "action=session.viewers.cutoff_evicted") {
			evicted = line
		}
	}
	for _, want := range []string{"principal=owner1", "session_id:mine-0", "revoked_session_id:bb"} {
		if !strings.Contains(evicted, want) {
			t.Errorf("eviction audit %q lacks %q", evicted, want)
		}
	}
}
