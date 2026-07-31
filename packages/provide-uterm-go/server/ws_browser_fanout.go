//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"

	"github.com/coder/websocket"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// browserFanoutSend handles a browser fanout_send frame. Port of the
// dispatch_browser_event fanout branch: it verifies the caller owns/can-access
// the group (get_group with the connection principal) and, on success,
// broadcasts to the group and writes a fanout_result control frame back. When
// the caller has no access, get_group returns nil and the handler silently does
// nothing (matching the Python bare `continue` — no response frame, socket
// stays live).
func (s *Server) browserFanoutSend(ctx context.Context, conn *websocket.Conn, bc *browserConn, msg map[string]any) {
	p := bc.principal
	if p == nil {
		p = serverauth.AnonymousPrincipal()
	}
	if !s.deps.Authz.IsAdmin(p) {
		s.writeFrame(ctx, conn, frames.MakeErrorFrame("admin role required"))
		return
	}
	subject := p.SubjectID
	groupID, _ := msg["group_id"].(string)
	data, _ := msg["data"].(string)

	// Ownership / access check: nil == caller doesn't own or have access.
	group := s.fanout.GetGroup(groupID, subject)
	if group == nil {
		return
	}
	if s.fanoutGovernanceUnsupported() {
		s.writeFrame(ctx, conn, frames.MakeErrorFrame(unsupportedFanoutGovernance))
		return
	}
	result, err := s.fanout.Send(ctx, groupID, data, p, 0, 0)
	if err != nil {
		s.writeFrame(ctx, conn, frames.MakeErrorFrame(err.Error()))
		return
	}
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
