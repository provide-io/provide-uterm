//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"testing"
)

// HasWorkerSocket answers the question every hijack turns on: is there a worker
// to pause? It has to tell an unknown worker from a known one whose socket has
// gone, because the two look the same to a caller and mean different things —
// one is a session nobody configured, the other one whose terminal went away.
func TestHasWorkerSocket(t *testing.T) {
	h := NewTermHub(TermHubConfig{Logger: discardLogger()})

	if h.HasWorkerSocket("nobody") {
		t.Fatal("an unregistered worker has no socket")
	}

	st := NewWorkerTermState()
	h.Registry.Put("w1", st)
	if h.HasWorkerSocket("w1") {
		t.Fatal("a registered worker with no socket is not attached")
	}

	st.WorkerWS = &recordingWorkerWS{}
	if !h.HasWorkerSocket("w1") {
		t.Fatal("a worker with a live socket is attached")
	}

	// And the moment the socket goes, so does the answer — which is what stops
	// a lease being granted against a terminal that is no longer there.
	if _, _ = h.Conn.DisconnectWorker(context.Background(), "w1"); h.HasWorkerSocket("w1") {
		t.Fatal("a disconnected worker is not attached")
	}
}
