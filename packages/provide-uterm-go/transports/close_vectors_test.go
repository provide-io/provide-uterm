//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package transports

// The typed close held to close_cases in spec/behavior_vectors.json, the
// fixture the Python, TypeScript and C# ports are tested against too. It is
// generated from the Python reference by scripts/generate_behavior_vectors.py;
// testdata/behavior_vectors.json is a byte-identical copy that
// scripts/check_protocol_drift.py keeps in step.

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

type closeFrameVector struct {
	Code   int    `json:"code"`
	Reason string `json:"reason"`
}

type expectedClose struct {
	Initiator CloseInitiator `json:"initiator"`
	Code      *int           `json:"code"`
	Reason    string         `json:"reason"`
}

type closeCases struct {
	Summary []struct {
		expectedClose
		Detail  string `json:"detail"`
		Summary string `json:"summary"`
	} `json:"summary"`
	WebSocket []struct {
		expectedClose
		Name             string            `json:"name"`
		Received         *closeFrameVector `json:"received"`
		Sent             *closeFrameVector `json:"sent"`
		ReceivedThenSent *bool             `json:"received_then_sent"`
		Detail           string            `json:"detail"`
	} `json:"websocket"`
	Telnet []eventCase `json:"telnet"`
	Chaos  []eventCase `json:"chaos"`
}

type eventCase struct {
	expectedClose
	Name      string `json:"name"`
	Event     string `json:"event"`
	Operation string `json:"operation"`
}

func loadCloseCases(t *testing.T) closeCases {
	t.Helper()
	// Prefer package testdata; fall back to repo-root spec via relative walk.
	candidates := []string{
		filepath.Join("testdata", "behavior_vectors.json"),
		filepath.Join("..", "..", "..", "spec", "behavior_vectors.json"),
	}
	var raw []byte
	var err error
	for _, path := range candidates {
		raw, err = os.ReadFile(path) //nolint:gosec // fixed test fixture paths
		if err == nil {
			break
		}
	}
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var v struct {
		CloseCases closeCases `json:"close_cases"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("parse vectors: %v", err)
	}
	return v.CloseCases
}

func expectVectorClose(t *testing.T, got TransportClose, want expectedClose) {
	t.Helper()
	if got.Initiator != want.Initiator || got.Reason != want.Reason {
		t.Errorf("close = %+v, want initiator %q reason %q", got, want.Initiator, want.Reason)
	}
	if (got.Code == nil) != (want.Code == nil) || (got.Code != nil && *got.Code != *want.Code) {
		t.Errorf("code = %v, want %v", got.Code, want.Code)
	}
}

func TestCloseVectorsSummary(t *testing.T) {
	cases := loadCloseCases(t).Summary
	if len(cases) == 0 {
		t.Fatal("no summary vectors")
	}
	for _, v := range cases {
		tc := TransportClose{Initiator: v.Initiator, Code: v.Code, Reason: v.Reason, Detail: v.Detail}
		if got := tc.Summary(); got != v.Summary {
			t.Errorf("Summary() = %q, want %q", got, v.Summary)
		}
	}
}

func TestCloseVectorsWebSocketAttribution(t *testing.T) {
	cases := loadCloseCases(t).WebSocket
	if len(cases) == 0 {
		t.Fatal("no websocket vectors")
	}
	frame := func(v *closeFrameVector) *wsCloseFrame {
		if v == nil {
			return nil
		}
		return &wsCloseFrame{code: v.Code, reason: v.Reason}
	}
	for _, v := range cases {
		t.Run(v.Name, func(t *testing.T) {
			// Only two frames have an order: null means false here.
			rcvdThenSent := v.ReceivedThenSent != nil && *v.ReceivedThenSent
			got := closeFromWSFrames(frame(v.Received), frame(v.Sent), rcvdThenSent, v.Detail)
			expectVectorClose(t, got, v.expectedClose)
			if got.Detail != v.Detail {
				t.Errorf("detail = %q, want %q", got.Detail, v.Detail)
			}
		})
	}
}

// socketErr stands in for the vector's socket event on a TCP read or write.
func socketErr(event, op string) error {
	errno := syscall.EPIPE
	if event == "reset" {
		errno = syscall.ECONNRESET
	}
	return &net.OpError{Op: op, Net: "tcp", Err: os.NewSyscallError(op, errno)}
}

// meetTelnet drives a telnet transport into the vector's event.
func meetTelnet(t *testing.T, v eventCase) error {
	t.Helper()
	switch {
	case v.Event == "rx_buffer_cap":
		c1, c2 := net.Pipe()
		t.Cleanup(func() { _ = c2.Close() })
		tr := telnetOn(c1)
		// An unterminated subnegotiation at the cap: the next byte pushes the
		// unconsumed buffer over it.
		tr.rxBuf = append([]byte{iacByte, cmdSB}, make([]byte, maxRxBufBytes)...)
		go func() { _, _ = c2.Write([]byte{'x'}) }()
		_, err := tr.Receive(context.Background(), 64, time.Second)
		return err
	case v.Event == "eof":
		_, err := telnetOn(&fakeConn{readErr: io.EOF}).Receive(context.Background(), 64, time.Second)
		return err
	case v.Operation == "receive":
		_, err := telnetOn(&fakeConn{readErr: socketErr(v.Event, "read")}).Receive(context.Background(), 64, time.Second)
		return err
	default:
		return telnetOn(&fakeConn{writeErr: socketErr(v.Event, "write")}).Send(context.Background(), []byte("x"))
	}
}

// The message prefix is this transport's own wording; the close is the shared contract.
var telnetVectorPrefixes = map[string]string{
	"eof/receive":           "connection closed by remote",
	"reset/receive":         "connection lost",
	"broken_pipe/receive":   "connection lost",
	"reset/send":            "send failed",
	"broken_pipe/send":      "send failed",
	"rx_buffer_cap/receive": "telnet receive buffer exceeded 262144 bytes (likely IAC SB without IAC SE)",
}

func TestCloseVectorsTelnet(t *testing.T) {
	cases := loadCloseCases(t).Telnet
	if len(cases) == 0 {
		t.Fatal("no telnet vectors")
	}
	for _, v := range cases {
		t.Run(v.Name, func(t *testing.T) {
			prefix, ok := telnetVectorPrefixes[v.Event+"/"+v.Operation]
			if !ok {
				t.Fatalf("no telnet driver for %s on %s", v.Event, v.Operation)
			}
			got := requireClose(t, meetTelnet(t, v), prefix, v.Initiator)
			expectVectorClose(t, got, v.expectedClose)
		})
	}
}

func TestCloseVectorsChaos(t *testing.T) {
	cases := loadCloseCases(t).Chaos
	if len(cases) == 0 {
		t.Fatal("no chaos vectors")
	}
	for _, v := range cases {
		t.Run(v.Name, func(t *testing.T) {
			c := NewChaosTransport(newStubTransport(), ChaosConfig{DisconnectEveryNReceives: 1, Label: "lbl"})
			_, err := c.Receive(context.Background(), 16, time.Millisecond)
			got := requireClose(t, err, "lbl: injected disconnect on receive #1", v.Initiator)
			expectVectorClose(t, got, v.expectedClose)
		})
	}
}
