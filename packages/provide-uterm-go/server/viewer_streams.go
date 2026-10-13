//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"sync"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// viewerRevokedEvent is the last SSE payload a share viewer's event stream gets
// before a revoke ends it (port of viewer_revocation.STREAM_REVOKED_EVENT).
const viewerRevokedEvent = `{"type":"viewer_access_revoked","reason":"viewer access revoked"}`

// shareViewerStreams tracks the open event streams of share viewers so a revoke
// can end them (port of viewer_revocation.ShareViewerStreams). The zero value
// is ready to use.
type shareViewerStreams struct {
	mu   sync.Mutex
	open map[string]map[chan struct{}]*serverauth.Principal
}

// register records a share viewer's stream and returns the channel a revoke
// closes, plus the release to call when the stream ends.
func (v *shareViewerStreams) register(sessionID string, p *serverauth.Principal) (chan struct{}, func()) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.open == nil {
		v.open = map[string]map[chan struct{}]*serverauth.Principal{}
	}
	if v.open[sessionID] == nil {
		v.open[sessionID] = map[chan struct{}]*serverauth.Principal{}
	}
	ended := make(chan struct{})
	v.open[sessionID][ended] = p
	return ended, func() {
		v.mu.Lock()
		defer v.mu.Unlock()
		delete(v.open[sessionID], ended)
		if len(v.open[sessionID]) == 0 {
			delete(v.open, sessionID)
		}
	}
}

// revoke ends every open stream on sessionID whose principal the cutoff
// refuses, unregistering it, and returns how many it ended.
func (v *shareViewerStreams) revoke(authz *serverauth.AuthorizationService, sessionID string) int {
	v.mu.Lock()
	defer v.mu.Unlock()
	ended := 0
	for ch, p := range v.open[sessionID] {
		if authz.ShareViewerRevoked(p, sessionID) {
			close(ch)
			delete(v.open[sessionID], ch)
			ended++
		}
	}
	return ended
}

// count reports the open share-viewer streams on sessionID.
func (v *shareViewerStreams) count(sessionID string) int {
	v.mu.Lock()
	defer v.mu.Unlock()
	return len(v.open[sessionID])
}
