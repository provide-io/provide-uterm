//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import "net/http"

// What a failed console, hub or session-registry operation tells the caller.
// Fixed text on purpose: the underlying error describes the server, not the
// request — a console's transport failure, a store fault, a dial error naming
// an internal address. The reference's handlers let these exceptions reach the
// framework's generic 500, so no detail crosses the wire there either. The
// error itself is logged under an event name instead.
const (
	guiOperationFailed  = "graphical console operation failed"
	hubOperationFailed  = "hub operation did not complete"
	sessionCreateFailed = "session could not be created"
)

// hideError logs err under event (with attrs) and answers with the fixed msg,
// written through the route's own envelope (detailError or bridgeError).
func (s *Server) hideError(
	w http.ResponseWriter, write func(http.ResponseWriter, int, string),
	status int, msg, event string, err error, attrs ...any,
) {
	s.logger.Error(event, append(attrs, "error", err.Error())...)
	write(w, status, msg)
}
