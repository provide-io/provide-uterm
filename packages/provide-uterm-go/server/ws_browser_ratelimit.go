//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// browserBuckets holds the two per-connection token buckets that rate-limit an
// inbound browser WebSocket: one for input frames and one for every other
// (control) frame. Port of the _browser_bucket / _browser_control_bucket pair
// created once per connection in websockets_impl.ws_browser_term.
type browserBuckets struct {
	input   *hub.TokenBucket
	control *hub.TokenBucket
}

// newBrowserBuckets builds the per-connection buckets sized from the hub's
// browser + control rate limits (Python:
// TokenBucket(hub.browser_rate_limit_per_sec) and
// TokenBucket(hub.browser_control_rate_limit_per_sec)). They share the server
// clock (the same clock the hub was built with) so refills are deterministic in
// tests.
func (s *Server) newBrowserBuckets() *browserBuckets {
	return &browserBuckets{
		input:   hub.NewTokenBucket(s.deps.Hub.BrowserRateLimitPerSec(), nil, s.clock),
		control: hub.NewTokenBucket(s.deps.Hub.BrowserControlRateLimitPerSec(), nil, s.clock),
	}
}

// browserErrSender is the subset of a browser connection the rate-limit path
// needs: send an already-control-framed payload. *browserConn satisfies it via
// the embedded wsBase.
type browserErrSender interface {
	SendText(ctx context.Context, payload string) error
}

// rateLimitBrowserFrame applies the per-frame token-bucket limits and reports
// whether the frame is admitted. On exceed it increments the matching metric,
// logs a warning, sends a {"type":"error","message":"rate_limited"} control
// frame, and drops the frame (returns false) — matching dispatch_browser_event's
// rate-limit path and its exact metric/message names. Input frames are limited
// by the input bucket; every other typed frame is limited by the control bucket.
// A frame with no "type" (mtype == "") is not rate-limited by either bucket,
// mirroring Python's `mtype is not None and mtype != "input"` guard.
func (s *Server) rateLimitBrowserFrame(
	ctx context.Context, sender browserErrSender, workerID, mtype string, b *browserBuckets,
) bool {
	switch {
	case mtype == "input" && !b.input.Allow():
		s.deps.Hub.Metric("ws_browser_rate_limited_total", 1)
		s.logger.Warn("ws_browser_rate_limited", "worker_id", workerID)
		s.sendControlErr(ctx, sender, "rate_limited")
		return false
	case mtype != "" && mtype != "input" && !b.control.Allow():
		s.deps.Hub.Metric("ws_browser_control_rate_limited_total", 1)
		s.logger.Warn("ws_browser_control_rate_limited", "worker_id", workerID, "mtype", mtype)
		s.sendControlErr(ctx, sender, "rate_limited")
		return false
	}
	return true
}

// sendControlErr encodes make_error_frame(message) as an inline control frame
// and sends it through sender, ignoring write errors (a failed send means the
// socket is gone; the recv loop will observe it). Mirrors
// websocket.send_text(encode_control_frame(make_error_frame(message))).
func (s *Server) sendControlErr(ctx context.Context, sender browserErrSender, message string) {
	payload, err := encodeFrameControl(frames.MakeErrorFrame(message))
	if err != nil {
		return
	}
	_ = sender.SendText(ctx, payload)
}
