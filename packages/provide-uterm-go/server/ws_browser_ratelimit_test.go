//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// recSender records the payloads sent to it (a stand-in browser socket for the
// rate-limit path, which only needs SendText).
type recSender struct {
	mu   sync.Mutex
	sent []string
}

func (r *recSender) SendText(_ context.Context, payload string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.sent = append(r.sent, payload)
	return nil
}

func (r *recSender) payloads() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.sent...)
}

// rlServer builds a test server whose hub is sized with the given browser +
// control per-second limits and driven by a frozen manual clock (no refill), so
// the token buckets are deterministic.
func rlServer(t *testing.T, inputRate, controlRate float64) *testServer {
	t.Helper()
	clk := hub.NewManualClock(1000)
	clk.SetMonotonic(0)
	return newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Clock = clk
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock:                         clk,
			OnMetric:                      deps.Metrics.Inc,
			Logger:                        deps.Logger,
			BrowserRateLimitPerSec:        inputRate,
			BrowserControlRateLimitPerSec: controlRate,
		})
	})
}

// decodeErrorFrame decodes a single control frame payload and asserts it is an
// error frame, returning its message.
func decodeErrorFrame(t *testing.T, payload string) string {
	t.Helper()
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	events, err := dec.Feed(payload)
	if err != nil {
		t.Fatalf("feed: %v", err)
	}
	for _, e := range events {
		if c, ok := e.(controlchannel.ControlChunk); ok {
			if c.Control["type"] != "error" {
				t.Fatalf("expected error frame, got %v", c.Control["type"])
			}
			msg, _ := c.Control["message"].(string)
			return msg
		}
	}
	t.Fatal("no control frame decoded")
	return ""
}

// TestRateLimitInputFramesDropOverBucket verifies input frames beyond the bucket
// are dropped with a rate_limited error frame + metric, and the control bucket
// is untouched.
func TestRateLimitInputFramesDropOverBucket(t *testing.T) {
	ts := rlServer(t, 2, 5) // input burst 2, control burst 5
	buckets := ts.srv.newBrowserBuckets()
	snd := &recSender{}
	ctx := context.Background()

	// First two input frames admitted.
	for i := 0; i < 2; i++ {
		if !ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "input", buckets) {
			t.Fatalf("input %d should be admitted", i)
		}
	}
	// Third input frame dropped.
	if ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "input", buckets) {
		t.Fatal("third input frame should be rate-limited")
	}
	got := snd.payloads()
	if len(got) != 1 {
		t.Fatalf("expected exactly one error frame, got %d", len(got))
	}
	if msg := decodeErrorFrame(t, got[0]); msg != "rate_limited" {
		t.Fatalf("error message = %q, want rate_limited", msg)
	}
	if n := ts.metrics.Snapshot()["ws_browser_rate_limited_total"]; n != 1 {
		t.Fatalf("ws_browser_rate_limited_total = %d, want 1", n)
	}
	// The control bucket was not consumed by input traffic.
	if !ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "presence_update", buckets) {
		t.Fatal("control frame should still be admitted (separate bucket)")
	}
	if n := ts.metrics.Snapshot()["ws_browser_control_rate_limited_total"]; n != 0 {
		t.Fatalf("control rate-limit metric should be 0, got %d", n)
	}
}

// TestRateLimitControlFramesSeparateBucket verifies control frames are limited by
// their own bucket, independent of the input bucket, with the control metric.
func TestRateLimitControlFramesSeparateBucket(t *testing.T) {
	ts := rlServer(t, 5, 1) // input burst 5, control burst 1
	buckets := ts.srv.newBrowserBuckets()
	snd := &recSender{}
	ctx := context.Background()

	// First control frame admitted, second dropped.
	if !ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "hijack_request", buckets) {
		t.Fatal("first control frame should be admitted")
	}
	if ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "hijack_request", buckets) {
		t.Fatal("second control frame should be rate-limited")
	}
	got := snd.payloads()
	if len(got) != 1 || decodeErrorFrame(t, got[0]) != "rate_limited" {
		t.Fatalf("expected one rate_limited error frame, got %v", got)
	}
	if n := ts.metrics.Snapshot()["ws_browser_control_rate_limited_total"]; n != 1 {
		t.Fatalf("ws_browser_control_rate_limited_total = %d, want 1", n)
	}
	// Input bucket still full (not consumed by control traffic).
	if !ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "input", buckets) {
		t.Fatal("input frame should be admitted (separate bucket)")
	}
	if n := ts.metrics.Snapshot()["ws_browser_rate_limited_total"]; n != 0 {
		t.Fatalf("input rate-limit metric should be 0, got %d", n)
	}
}

// TestRateLimitInputDroppedThroughRecvLoop drives a real browser WebSocket and
// exceeds the (burst-1) input bucket, verifying the recv loop drops the extra
// input frames and answers with a rate_limited error frame end-to-end.
func TestRateLimitInputDroppedThroughRecvLoop(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		// Reuse the real clock deps.Clock; only shrink the input bucket so a
		// couple of rapid frames overrun it (refill over the send window is
		// negligible). Control stays generous so the handshake is unaffected.
		deps.Hub = hub.NewTermHub(hub.TermHubConfig{
			Clock:                         deps.Clock,
			OnMetric:                      deps.Metrics.Inc,
			Logger:                        deps.Logger,
			BrowserRateLimitPerSec:        1,
			BrowserControlRateLimitPerSec: 50,
		})
	})
	ts.srv.MarkReady()
	ts.reg.add("rl", "admin1", "public")
	httpSrv := httptest.NewServer(ts.srv.Handler())
	defer httpSrv.Close()
	wsBase := "ws" + strings.TrimPrefix(httpSrv.URL, "http")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	bc := dialBrowser(t, ctx, wsBase+"/ws/browser/rl/term", "admin1", "admin")
	defer func() { _ = bc.conn.Close(websocket.StatusNormalClosure, "") }()
	bc.waitFrame(t, "hello", 5*time.Second)

	// Burst 1: the first input consumes the token; the rest are rate-limited.
	for i := 0; i < 5; i++ {
		bc.send(t, ctx, map[string]any{"type": "input", "data": "x"})
	}
	bc.waitFrameWhere(t, "error", 5*time.Second, func(f map[string]any) bool {
		msg, _ := f["message"].(string)
		return msg == "rate_limited"
	})
}

// TestRateLimitUntypedFrameNotLimited verifies a frame with no "type" is not
// rate-limited by either bucket (Python's mtype-is-None guard).
func TestRateLimitUntypedFrameNotLimited(t *testing.T) {
	ts := rlServer(t, 1, 1) // both buckets: burst 1
	buckets := ts.srv.newBrowserBuckets()
	snd := &recSender{}
	ctx := context.Background()

	// Many untyped frames: never limited, buckets untouched.
	for i := 0; i < 5; i++ {
		if !ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "", buckets) {
			t.Fatalf("untyped frame %d should never be rate-limited", i)
		}
	}
	if len(snd.payloads()) != 0 {
		t.Fatal("untyped frames must not emit error frames")
	}
	// Both buckets are still full.
	if !ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "input", buckets) {
		t.Fatal("input bucket should be full")
	}
	if !ts.srv.rateLimitBrowserFrame(ctx, snd, "w", "presence_update", buckets) {
		t.Fatal("control bucket should be full")
	}
}
