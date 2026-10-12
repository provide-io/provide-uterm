//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
	uptty "github.com/provide-io/provide-uterm/packages/provide-uterm-go/pty"
)

// workerSink is the hub end of a worker socket, reduced to recording the
// control frames the worker sends it.
type workerSink struct {
	mu   sync.Mutex
	ctrl []map[string]any
}

func newWorkerSink(t *testing.T) (*workerSink, string) {
	t.Helper()
	sink := &workerSink{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		defer func() { _ = c.CloseNow() }()
		decoder := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
		for {
			typ, raw, err := c.Read(r.Context())
			if err != nil {
				return
			}
			chunk := string(raw)
			if typ == websocket.MessageBinary {
				chunk = controlchannel.WSBytesToChannelStr(raw)
			}
			events, err := decoder.Feed(chunk)
			if err != nil {
				return
			}
			sink.mu.Lock()
			for _, ev := range events {
				if ctrl, ok := ev.(controlchannel.ControlChunk); ok {
					sink.ctrl = append(sink.ctrl, ctrl.Control)
				}
			}
			sink.mu.Unlock()
		}
	}))
	t.Cleanup(srv.Close)
	return sink, srv.URL
}

func (s *workerSink) snapshotAt(cols, rows int) map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, f := range s.ctrl {
		// Compared as text: the decoder's number type is not this test's business.
		if f["type"] == "snapshot" && fmt.Sprint(f["cols"]) == fmt.Sprint(cols) && fmt.Sprint(f["rows"]) == fmt.Sprint(rows) {
			return f
		}
	}
	return nil
}

func (s *workerSink) helloed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, f := range s.ctrl {
		if f["type"] == "worker_hello" {
			return true
		}
	}
	return false
}

// A pty_capture session hosted by the Go server, end to end on the worker side:
// a geometry frame on the capture wire reaches the hub as a snapshot at the new
// size, unasked -- the frame the hub broadcasts to every viewer and watcher --
// with the screen redrawn at that size.
func TestCaptureGeometryRepaintsTheHubAtTheNewSize(t *testing.T) {
	sock := shortCaptureDir(t) + "/cap.sock"
	conn, err := connectors.Build("cap", "Cap", "pty_capture", map[string]any{
		"socket_path": sock, "cols": 80, "rows": 24,
	})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := conn.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	defer func() { _ = conn.Stop(context.Background()) }()

	sink, hubURL := newWorkerSink(t)
	br := bridge.New(bridge.Config{
		Worker: &sessionWorker{conn: conn}, WorkerID: "cap", ManagerURL: hubURL, Encoding: "latin-1",
	})
	br.Start(ctx)
	defer br.Stop()
	waitFor(t, "the worker socket", sink.helloed)

	writeCaptureFrame(t, sock, uptty.ChannelStdout, "\x1b[2J\x1b[H"+strings.Repeat("x", 90))
	waitFor(t, "captured output on the screen", func() bool {
		return strings.Contains(conn.Snapshot().Screen, "xxxxxxxxxx")
	})

	geo, err := net.Dial("unix", sock)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	frame := []byte{uptty.ChannelGeometry, 0, 0, 0, 4, 0, 0, 0, 0}
	binary.BigEndian.PutUint16(frame[5:], 100)
	binary.BigEndian.PutUint16(frame[7:], 40)
	_, _ = geo.Write(frame)
	_ = geo.Close()

	var snap map[string]any
	waitFor(t, "a snapshot at 100x40", func() bool { snap = sink.snapshotAt(100, 40); return snap != nil })
	screen, _ := snap["screen"].(string)
	if first := strings.SplitN(screen, "\n", 2)[0]; strings.TrimRight(first, " ") != strings.Repeat("x", 90) {
		t.Fatalf("first row = %q, want the 90 x's redrawn on one row", first)
	}
}
