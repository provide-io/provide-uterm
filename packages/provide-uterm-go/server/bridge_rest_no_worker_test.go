//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"
)

// TestHijackSendStepNoWorker covers the SendWorker→false arms (409) when the
// worker socket is gone but the REST lease remains (DisconnectWorker would also
// clear the lease, so we only nil WorkerWS).
func TestHijackSendStepNoWorker(t *testing.T) {
	ts := newTestServer(t, nil)
	hid := acquireHijack(t, ts, "gone")
	st := ts.hub.Registry.Get("gone")
	if st == nil {
		t.Fatal("missing worker state")
	}
	st.WorkerWS = nil
	if rec := ts.do("POST", "/worker/gone/hijack/"+hid+"/step", "", adminHeaders()); rec.Code != http.StatusConflict {
		t.Fatalf("step no worker: %d %s", rec.Code, rec.Body.String())
	}
	// Re-acquire is not needed — lease still holds after step conflict.
	if rec := ts.do("POST", "/worker/gone/hijack/"+hid+"/send", `{"keys":"x"}`, adminHeaders()); rec.Code != http.StatusConflict {
		t.Fatalf("send no worker: %d %s", rec.Code, rec.Body.String())
	}
}
