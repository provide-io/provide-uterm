//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"

	"github.com/coder/websocket"
)

// browserFanoutSend handles a browser fanout_send frame. Port of the
// dispatch_browser_event fanout branch: it verifies the caller owns/can-access
// the group (get_group with the connection principal) and, on success,
// broadcasts to the group and writes a fanout_result control frame back. When
// the caller has no access, get_group returns nil and the handler silently does
// nothing (matching the Python bare `continue` — no response frame, socket
// stays live).
func (s *Server) browserFanoutSend(ctx context.Context, conn *websocket.Conn, bc *browserConn, msg map[string]any) {
	subject := "anonymous"
	if bc.principal != nil {
		subject = bc.principal.SubjectID
	}
	groupID, _ := msg["group_id"].(string)
	data, _ := msg["data"].(string)

	// Ownership / access check: nil == caller doesn't own or have access.
	if s.fanout.GetGroup(groupID, subject) == nil {
		return
	}
	result := s.fanout.Send(ctx, groupID, data, subject, 0, 0)
	frame := map[string]any{
		"type":               "fanout_result",
		"group_id":           result.GroupID,
		"send_id":            result.SendID,
		"results":            result.ResultMaps(),
		"divergent_sessions": result.DivergentSessions,
		"failed_sessions":    result.FailedSessions,
	}
	payload, err := encodeControlMap(frame)
	if err != nil {
		return
	}
	_ = conn.Write(ctx, websocket.MessageText, []byte(payload))
}
