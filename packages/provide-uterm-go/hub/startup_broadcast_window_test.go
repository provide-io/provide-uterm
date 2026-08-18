//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

// Frames broadcast while a browser is still starting up must not be lost.
//
// A browser registers with deferBroadcast so its hello, hijack_state and
// presence_sync arrive before anything else; until it is activated it is not
// in the broadcast set at all, and what was broadcast meanwhile used to be
// dropped. That is right for frames the startup sequence already carries and
// wrong for the inspect channel, which has no replay: the browser builds that
// list from nothing, so a dropped http_req is a row missing for the rest of
// the session. Port of test_startup_broadcast_window.py.

import (
	"errors"
	"strings"
	"testing"
)

func httpReqFrame(id, url string) map[string]any {
	return map[string]any{
		"type":     "http_req",
		"id":       id,
		"method":   "GET",
		"url":      url,
		"_channel": "http",
	}
}

// sentURLs returns the order the browser actually received request URLs in.
func sentURLs(ws *fakeBrowserWS, urls ...string) []string {
	var seen []string
	for _, payload := range ws.payloads() {
		for _, url := range urls {
			if strings.Contains(payload, url) {
				seen = append(seen, url)
			}
		}
	}
	return seen
}

func TestInspectFrameDuringStartupIsDeliveredOnActivation(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)

	_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r1", "/api/users"))
	if got := len(ws.payloads()); got != 0 {
		t.Fatalf("a browser mid-startup must not be written to yet, got %d payload(s)", got)
	}

	h.Conn.ActivateBrowserBroadcasts(bg(), "w1", ws)

	if got := sentURLs(ws, "/api/users"); len(got) != 1 {
		t.Fatalf("buffered inspect frame not delivered on activation, got %v", got)
	}
}

func TestBufferedInspectFramesKeepTheirOrder(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)

	_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r0", "/api/zero"))
	_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r1", "/api/one"))
	_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r2", "/api/two"))
	h.Conn.ActivateBrowserBroadcasts(bg(), "w1", ws)

	got := sentURLs(ws, "/api/zero", "/api/one", "/api/two")
	want := []string{"/api/zero", "/api/one", "/api/two"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("buffered frames reordered: got %v want %v", got, want)
	}
}

func TestTerminalOutputFromTheWindowIsNotReplayed(t *testing.T) {
	// The hello's initial_snapshot already covers it; replaying prints twice.
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)

	_ = h.Router.Broadcast(bg(), "w1", map[string]any{"type": "term", "data": "ls -la\r\n"})
	h.Conn.ActivateBrowserBroadcasts(bg(), "w1", ws)

	if got := len(ws.payloads()); got != 0 {
		t.Fatalf("terminal output must not be replayed, got %d payload(s)", got)
	}
}

func TestActivatedBrowserReceivesInspectFramesDirectly(t *testing.T) {
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)
	h.Conn.ActivateBrowserBroadcasts(bg(), "w1", ws)

	_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r1", "/api/users"))

	if got := sentURLs(ws, "/api/users"); len(got) != 1 {
		t.Fatalf("activated browser missed a live inspect frame, got %v", got)
	}
	h.lock.Lock()
	_, buffered := h.startupPendingFrames[ws]
	h.lock.Unlock()
	if buffered {
		t.Fatal("an activated browser must not accumulate a backlog")
	}
}

func TestStartupBufferIsCappedRatherThanUnbounded(t *testing.T) {
	// A browser that never activates must not be able to grow this forever.
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)

	for i := 0; i < startupBufferMaxFrames+25; i++ {
		_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r", "/api/x"))
	}

	h.lock.Lock()
	queued := len(h.startupPendingFrames[ws])
	h.lock.Unlock()
	if queued != startupBufferMaxFrames {
		t.Fatalf("buffer not capped: got %d want %d", queued, startupBufferMaxFrames)
	}
}

func TestDisconnectingBrowserDropsItsBacklog(t *testing.T) {
	// Nothing will ever flush it, so holding it is a leak.
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)
	_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r1", "/api/users"))

	_, _ = h.Conn.CleanupBrowserDisconnect(bg(), "w1", ws, false)

	h.lock.Lock()
	_, buffered := h.startupPendingFrames[ws]
	h.lock.Unlock()
	if buffered {
		t.Fatal("a disconnected browser kept its backlog")
	}
}

func TestSocketThatCannotTakeItsBacklogDropsIt(t *testing.T) {
	// Pending is the right resting state for a socket that just failed a
	// write: the broadcast path skips it rather than retrying into a dead
	// connection, and the disconnect path clears both.
	h, _ := newTestHub(t, nil)
	ws := newBrowserWS("b")
	_, _ = h.Conn.RegisterBrowser(bg(), "w1", ws, "viewer", true)
	_ = h.Router.Broadcast(bg(), "w1", httpReqFrame("r1", "/api/users"))
	ws.failSend = errors.New("socket gone")

	h.Conn.ActivateBrowserBroadcasts(bg(), "w1", ws)

	h.lock.Lock()
	_, buffered := h.startupPendingFrames[ws]
	stillPending := h.startupPendingBrowsers[ws]
	h.lock.Unlock()
	if buffered {
		t.Fatal("a failed flush must drop the backlog")
	}
	if !stillPending {
		t.Fatal("a socket that failed a write must stay skipped")
	}
}
