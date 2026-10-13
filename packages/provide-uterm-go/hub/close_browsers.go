//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"sync"
)

// CloseBrowsersWhere closes every browser of workerID for which match(ws, role)
// holds, with the given WebSocket close code and reason, and returns how many
// it closed. A browser conn without a close surface (BrowserCloser) is skipped.
//
// The browsers are chosen under the hub lock and closed outside it, all at
// once: a close waits on the peer's close frame, and one unresponsive peer must
// not hold up the rest. Used to end live share-viewer sockets when a session's
// viewer access is revoked (port of viewer_revocation.close_revoked_viewers).
func (h *TermHub) CloseBrowsersWhere(
	ctx context.Context, workerID string, match func(ws BrowserConn, role string) bool, code int, reason string,
) int {
	h.lock.Lock()
	var targets []BrowserCloser
	if st := h.registry.Get(workerID); st != nil {
		for ws, role := range st.Browsers {
			if closer, ok := ws.(BrowserCloser); ok && match(ws, role) {
				targets = append(targets, closer)
			}
		}
	}
	h.lock.Unlock()

	var wg sync.WaitGroup
	for _, closer := range targets {
		wg.Add(1)
		go func(c BrowserCloser) {
			defer wg.Done()
			_ = c.Close(ctx, code, reason) //nolint:errcheck // a socket already closing has nothing left to tell
		}(closer)
	}
	wg.Wait()
	return len(targets)
}
