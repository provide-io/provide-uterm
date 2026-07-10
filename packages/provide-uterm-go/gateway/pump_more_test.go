//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"bytes"
	"context"
	"testing"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// TestRunGatewaySessionContextCancelDuringDelay covers the ctx.Done branch of
// the reconnect-delay select: a transient drop schedules a delayed reconnect,
// and cancelling the context during that wait returns immediately rather than
// sleeping out the (long) delay.
func TestRunGatewaySessionContextCancelDuringDelay(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	go func() { time.Sleep(100 * time.Millisecond); cancel() }()
	start := time.Now()
	runGatewaySession(ctx, sessionParams{
		wsURL:           "wss://h/ws",
		pump:            func(context.Context, string) (int, error) { return -1, nil }, // transient drop
		clientConnected: func() bool { return true },
		showReconnect:   func() {},
		st:              &controlState{},
		maxReconnects:   3,
		reconnectDelay:  10 * time.Second,
		maxRedirects:    5,
	})
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("ctx cancel should short-circuit the reconnect delay, took %v", elapsed)
	}
}

// TestApplyRedirectParseError covers the url.Parse failure branch: a current
// URL containing a control byte is unparseable, so no redirect is applied even
// though the path itself is well-formed.
func TestApplyRedirectParseError(t *testing.T) {
	if got, ok := applyRedirect("ws://host\x7f/ws", "/new"); ok || got != "" {
		t.Errorf("unparseable current URL should fail, got (%q,%v)", got, ok)
	}
}

// TestDispatchTextMessage drives dispatchTextMessage directly through its three
// outcomes: a data chunk written to the client, a redirect frame ending the
// pump, and a malformed frame that decodes to an error (done=false).
func TestDispatchTextMessage(t *testing.T) {
	newCfg := func(st *controlState, sink *bytes.Buffer) pumpConfig {
		return pumpConfig{
			st:             st,
			writeClient:    func(b []byte) error { sink.Write(b); return nil },
			writeTransform: func(b []byte) []byte { return b },
		}
	}

	// Data chunk → forwarded to the client via writeTransform.
	var sink bytes.Buffer
	st := &controlState{}
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	msg := controlchannel.EncodeTerminalData(controlchannel.WSBytesToChannelStr([]byte("hello")))
	if done := dispatchTextMessage(msg, dec, newCfg(st, &sink)); done {
		t.Fatal("data chunk should not end the pump")
	}
	if sink.String() != "hello" {
		t.Fatalf("client sink = %q, want hello", sink.String())
	}

	// Redirect control frame → done=true and st.redirect set.
	st2 := &controlState{}
	dec2 := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	frame, err := controlchannel.EncodeControlFrame(map[string]any{"type": "redirect", "path": "/game"})
	if err != nil {
		t.Fatal(err)
	}
	if done := dispatchTextMessage(frame, dec2, newCfg(st2, &bytes.Buffer{})); !done {
		t.Fatal("redirect frame should end the pump")
	}
	if st2.redirect != "/game" {
		t.Fatalf("redirect = %q", st2.redirect)
	}

	// Malformed frame (valid header, invalid JSON payload) → decode error.
	st3 := &controlState{}
	dec3 := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	bad := controlchannel.DLE + controlchannel.STX + "00000002:{{"
	if done := dispatchTextMessage(bad, dec3, newCfg(st3, &bytes.Buffer{})); done {
		t.Fatal("malformed frame should not end the pump")
	}
}

// TestSendFrameEncodeError covers sendFrame's early error return when the frame
// cannot be JSON-encoded (a channel value is not serializable). The encode
// fails before the connection is touched, so a nil conn is safe here.
func TestSendFrameEncodeError(t *testing.T) {
	if err := sendFrame(context.Background(), nil, map[string]any{"bad": make(chan int)}); err == nil {
		t.Fatal("non-serializable frame should error")
	}
}
