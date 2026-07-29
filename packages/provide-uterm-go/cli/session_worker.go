//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"encoding/json"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/termsession"
)

// sessionWorker adapts one hosted session's connector to the pair of interfaces
// [bridge.TermBridge] drives — [bridge.Worker] and [bridge.Session].
//
// It is the missing half of the reference's HostedSessionRuntime (runtime.py
// _run): the reference starts the connector and then dials its own server at
// /ws/worker/{session_id}/term, so a configured session is not merely a live
// terminal but a worker the hub knows about. Everything the hub does to a
// session — pausing it for a hijack, asking it for a snapshot, forwarding a
// lease-holder's keystrokes — travels that socket, so a port that starts the
// connector and stops there has a session nobody can take a lease on.
//
// One value serves as both interfaces so that its identity is stable:
// [bridge.TermBridge.AttachSession] registers its watch once per session object
// and compares by equality, and a fresh adapter per call would register a new
// watch on every snapshot request.
type sessionWorker struct {
	conn connectors.Connector
}

var (
	_ bridge.Worker  = (*sessionWorker)(nil)
	_ bridge.Session = (*sessionWorker)(nil)
)

// Session reports the live session, or nil while the connector has none. The
// nil is returned explicitly rather than as a typed nil so the bridge's
// `session == nil` checks see it.
func (w *sessionWorker) Session() bridge.Session {
	if w.conn.Session() == nil {
		return nil
	}
	return w
}

// SetHijacked pauses (true) or resumes (false) the connector, which is what the
// hub's pause/resume control frames mean on the worker side.
func (w *sessionWorker) SetHijacked(_ context.Context, enabled bool) error {
	action := "resume"
	if enabled {
		action = "pause"
	}
	return w.conn.HandleControl(action)
}

// RequestStep lets a hijacked worker past one checkpoint.
func (w *sessionWorker) RequestStep(_ context.Context) error {
	return w.conn.HandleControl("step")
}

// AddWatch forwards raw terminal output to the bridge. The two WatchFunc types
// are the same signature, so the conversion is free.
func (w *sessionWorker) AddWatch(fn bridge.WatchFunc) {
	sess := w.conn.Session()
	if sess == nil {
		return
	}
	sess.AddWatch(termsession.WatchFunc(fn))
}

// Send delivers a lease holder's keystrokes to the upstream terminal.
func (w *sessionWorker) Send(ctx context.Context, data string) error {
	return w.conn.HandleInput(ctx, data)
}

// SetSize is a no-op: the Connector interface carries no resize, so there is
// nothing to forward a hub resize frame to. Reporting success rather than an
// error matches what the bridge does with either (it logs and continues), and
// keeps a resize from looking like a broken worker.
func (w *sessionWorker) SetSize(_ context.Context, _, _ int) error { return nil }

// Snapshot returns the emulator's current screen in the wire shape the bridge
// sends on. The JSON round-trip is the same one the registry's LastSnapshot
// uses, so the field names come from one place — the session.Snapshot tags.
//
// Neither half of the round-trip is checked because neither can fail:
// session.Snapshot is a struct of strings, numbers and bools, and what marshals
// from that always unmarshals into a map.
func (w *sessionWorker) Snapshot() map[string]any {
	if w.conn.Session() == nil {
		return nil
	}
	raw, _ := json.Marshal(w.conn.Snapshot())
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	return out
}
