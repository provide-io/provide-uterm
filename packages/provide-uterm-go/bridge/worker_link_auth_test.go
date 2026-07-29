//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// A hub configured with a worker token closes any worker handshake that arrives
// without it. A bridge that could not carry the token would leave such a
// deployment hosting sessions no client could ever take a lease on, so the
// token has to be on the handshake itself — there is no later request to put it
// on.
func TestTermBridgePresentsTheWorkerBearerToken(t *testing.T) {
	seen := make(chan string, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case seen <- r.Header.Get("Authorization"):
		default:
		}
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		_ = conn.CloseNow()
	}))
	defer srv.Close()

	br := New(Config{
		Worker:      &mockWorker{},
		WorkerID:    "w1",
		ManagerURL:  srv.URL,
		BearerToken: "worker-token",
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	br.Start(ctx)
	defer br.Stop()

	select {
	case auth := <-seen:
		if auth != "Bearer worker-token" {
			t.Fatalf("Authorization = %q, want the worker bearer token", auth)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the bridge never dialed")
	}
}
