//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"
)

// TestHijackSendReleaseInvalidID covers the GetRestSession==nil (404) branches
// of handleHijackSend and handleHijackRelease: a well-formed but unknown hijack
// id on a registered worker.
func TestHijackSendReleaseInvalidID(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.setupWorker(t, "w1")
	hdr := adminHeaders()

	if rec := ts.do("POST", "/worker/w1/hijack/deadbeef-0000/send", `{"keys":"x"}`, hdr); rec.Code != http.StatusNotFound {
		t.Fatalf("send invalid hijack: want 404, got %d %s", rec.Code, rec.Body.String())
	}
	if rec := ts.do("POST", "/worker/w1/hijack/deadbeef-0000/release", "", hdr); rec.Code != http.StatusNotFound {
		t.Fatalf("release invalid hijack: want 404, got %d %s", rec.Code, rec.Body.String())
	}
}

// TestDisconnectWorkerNotFound covers the 404 branch of handleDisconnectWorker.
func TestDisconnectWorkerNotFound(t *testing.T) {
	ts := newTestServer(t, nil)
	rec := ts.do("POST", "/worker/ghost/disconnect_worker", "", adminHeaders())
	if rec.Code != http.StatusNotFound {
		t.Fatalf("disconnect ghost: want 404, got %d %s", rec.Code, rec.Body.String())
	}
}
