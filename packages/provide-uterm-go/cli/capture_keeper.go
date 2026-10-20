//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"context"
	"sync"
	"time"

	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/bridge"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/capturestate"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/connectors"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/defaults"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/server"
)

// The registry's half of [session_persistence] (package capturestate): which
// sessions are kept, the definition written as it changes, the screen as it
// settles and once more as the session stops, and the restore at startup.
// Port of the reference's ScreenKeeper and restore_capture_sessions.

// screenSaveInterval is how often, at most, a busy session's screen is
// written. A screen is saved once output pauses (the recording's settle
// trigger); this bounds output that never pauses to one write a second, each
// a rename of a synced file. Port of SCREEN_SAVE_INTERVAL_S.
func screenSaveInterval() time.Duration { return time.Second }

// captureKeeper keeps one capture session's screen in its store record as the
// screen changes: a second sink on the recording's settle trigger, with none
// of the recording's gates (it runs with no recording, past the recording's
// quota, and keeps the screen unredacted). It is a bridge.Observer, so the
// bridge's settle loop drives it. Safe for concurrent use.
type captureKeeper struct {
	store    *capturestate.Store
	id       string
	instance string
	now      func() time.Time

	mu           sync.Mutex
	conn         connectors.Connector
	pendingSince time.Time // zero: no output waiting for a screen
	lastOutputAt time.Time
	lastSavedAt  time.Time
	lastSaved    string
}

var _ bridge.Observer = (*captureKeeper)(nil)

// attach is the connector whose screen the keeper takes from now on.
func (k *captureKeeper) attach(conn connectors.Connector) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.conn = conn
}

func (k *captureKeeper) AttemptStarted()                      {}
func (k *captureKeeper) Connected() error                     { return nil }
func (k *captureKeeper) WireReceived(string) error            { return nil }
func (k *captureKeeper) ControlReceived(map[string]any) error { return nil }
func (k *captureKeeper) InputReceived(string) error           { return nil }
func (k *captureKeeper) AttemptEnded(error)                   {}
func (k *captureKeeper) FrameSent(_ string, frame map[string]any) error {
	if frame["type"] == "term" {
		k.mu.Lock()
		now := k.now()
		k.lastOutputAt = now
		if k.pendingSince.IsZero() {
			k.pendingSince = now
		}
		k.mu.Unlock()
	}
	return nil
}

// RecordSettledScreen saves the screen when the settle trigger says one is
// owed: output is waiting, it has paused for the settle period or waited an
// interval, and the last save is an interval old. Port of
// settled_screen_due, which the reference shares with its recording.
func (k *captureKeeper) RecordSettledScreen(func() map[string]any) error {
	k.mu.Lock()
	now := k.now()
	interval := screenSaveInterval()
	settle := time.Duration(defaults.RecordedScreenSettleS * float64(time.Second))
	due := !k.pendingSince.IsZero() && now.Sub(k.lastSavedAt) >= interval &&
		(now.Sub(k.lastOutputAt) >= settle || now.Sub(k.pendingSince) >= interval)
	if due {
		k.lastSavedAt = now
		k.pendingSince = time.Time{}
	}
	k.mu.Unlock()
	if due {
		k.save(false)
	}
	return nil
}

// saveNow saves the screen whatever the trigger says: the session is stopping.
func (k *captureKeeper) saveNow() { k.save(true) }

// save takes the connector's screen and, when it changed or always is set,
// hands it to the store. The screen is read without k.mu held, since it reads
// the session's emulator.
func (k *captureKeeper) save(always bool) {
	k.mu.Lock()
	conn := k.conn
	k.mu.Unlock()
	state, ok := conn.(connectors.CaptureState)
	if !ok || conn.Session() == nil {
		return
	}
	sess := conn.Session()
	screen := sess.ANSIScreen()
	emu := sess.Emulator()
	k.mu.Lock()
	changed := always || screen != k.lastSaved
	k.lastSaved = screen
	k.mu.Unlock()
	if changed {
		k.store.SaveScreen(k.id, screen, emu.Cols(), emu.Rows(), state.Scrollback())
	}
}

// multiObserver hands every call to each observer in turn; the first error is
// returned. The recorder and the capture keeper both watch one bridge.
type multiObserver []bridge.Observer

func (m multiObserver) AttemptStarted() {
	for _, o := range m {
		o.AttemptStarted()
	}
}

func (m multiObserver) AttemptEnded(err error) {
	for _, o := range m {
		o.AttemptEnded(err)
	}
}

func (m multiObserver) each(call func(bridge.Observer) error) error {
	var first error
	for _, o := range m {
		if err := call(o); err != nil && first == nil {
			first = err
		}
	}
	return first
}

func (m multiObserver) Connected() error {
	return m.each(func(o bridge.Observer) error { return o.Connected() })
}

func (m multiObserver) FrameSent(payload string, frame map[string]any) error {
	return m.each(func(o bridge.Observer) error { return o.FrameSent(payload, frame) })
}

func (m multiObserver) WireReceived(text string) error {
	return m.each(func(o bridge.Observer) error { return o.WireReceived(text) })
}

func (m multiObserver) ControlReceived(msg map[string]any) error {
	return m.each(func(o bridge.Observer) error { return o.ControlReceived(msg) })
}

func (m multiObserver) InputReceived(data string) error {
	return m.each(func(o bridge.Observer) error { return o.InputReceived(data) })
}

func (m multiObserver) RecordSettledScreen(screen func() map[string]any) error {
	return m.each(func(o bridge.Observer) error { return o.RecordSettledScreen(screen) })
}

// SetCaptureState wires the store PAM capture sessions are kept in
// ([session_persistence]); nil, the default, keeps none. Safe to call once,
// at server boot, before anything is created or restored.
func (r *SessionRegistryImpl) SetCaptureState(store *capturestate.Store) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.capture = store
}

// keeperFor returns the session's keeper, building it on first use; nil for
// a session the store does not keep, or with no store. Caller holds r.mu.
func (r *SessionRegistryImpl) keeperFor(e *sessionEntry) *captureKeeper {
	if r.capture == nil || !r.capture.Tracks(e.def) {
		return nil
	}
	if e.keeper == nil {
		e.keeper = &captureKeeper{
			store: r.capture, id: e.def.SessionID, now: time.Now,
			instance: r.capture.Track(e.def, newRecordingInstance()),
		}
	}
	return e.keeper
}

// setDefinitionLocked records a kept session's definition as it now stands,
// and returns what writes it, for the caller to run once r.mu is released;
// nil for any other session. Caller holds r.mu.
func (r *SessionRegistryImpl) setDefinitionLocked(e *sessionEntry) func() {
	if r.keeperFor(e) == nil {
		return nil
	}
	store, id := r.capture, e.def.SessionID
	store.SetDefinition(e.def)
	return func() { store.Flush(id) }
}

// prepareCapture readies a kept session's connector before it starts: its
// scrollback, and the screen a restart left, painted back by the first start
// that succeeds (StartSession marks it done).
func (r *SessionRegistryImpl) prepareCapture(id string, conn connectors.Connector) {
	r.mu.Lock()
	store := r.capture
	var keeper *captureKeeper
	if e, ok := r.entries[id]; ok {
		keeper = r.keeperFor(e)
	}
	r.mu.Unlock()
	state, isCapture := conn.(connectors.CaptureState)
	if keeper == nil || !isCapture {
		return
	}
	// The scrollback kept so far carries over every start, a stop and start
	// in this process as much as a restart.
	state.KeepScrollback(store.ScrollbackLines(), store.Scrollback(id))
	if screen, cols, rows, restore := store.PendingRestore(id); restore {
		state.RestoreScreen(screen, cols, rows)
		// Painting it back changes nothing worth writing.
		keeper.mu.Lock()
		keeper.lastSaved = screen
		keeper.mu.Unlock()
	}
}

// saveLastScreen takes a stopping session's last screen, before its connector
// goes: what a restart paints back, or what it leaves as its previous one.
func saveLastScreen(k *captureKeeper) {
	if k != nil {
		k.saveNow()
	}
}

// RestoreCaptureSessions re-creates every persisted capture session whose
// login is still alive, and ends the rest. Run once at startup, after the
// control plane's migration and before sessions auto-start. A file is
// trusted no further than a request would be: its socket must pass the
// confinement a PAM-created capture session passes (socketAllowed), and its
// login must be the same process, by pid and start ticks. Anything else has
// ended as far as this server can tell, and becomes a previous session.
//
// Unlike CreateSession the recording under the id is kept: it is this
// session's, and the session goes on recording under the same instance.
func (r *SessionRegistryImpl) RestoreCaptureSessions(ctx context.Context, socketAllowed func(string) bool) {
	r.mu.Lock()
	store := r.capture
	r.mu.Unlock()
	if store == nil {
		return
	}
	store.LoadPrevious()
	if !store.Persist() {
		return
	}
	logger := ptel.GetLogger(ctx, "provide.uterm.server.capture_state")
	for _, rec := range store.LoadLive() {
		id := rec.SessionID()
		socket, _ := rec.Definition.ConnectorConfig["socket_path"].(string)
		pid, ticks, _ := capturestate.LoginIdentity(id)
		switch {
		case !socketAllowed(socket):
			logger.Warn("capture_state_restore_refused", "session_id", id, "reason", "socket_not_confined")
			store.EndRecord(rec)
		case !server.PamLoginAlive(pid, ticks):
			logger.Info("capture_state_login_gone", "session_id", id)
			store.EndRecord(rec)
		case r.adoptRestored(rec):
			logger.Info("capture_state_restored", "session_id", id)
		}
	}
}

// adoptRestored inserts a restored record's session; false when one exists
// under its id already, or the id is being torn down, which keeps the record
// where it is for the next start.
func (r *SessionRegistryImpl) adoptRestored(rec *capturestate.Record) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	id := rec.SessionID()
	if _, exists := r.entries[id]; exists {
		return false
	}
	if _, held := r.retiring[id]; held {
		return false
	}
	r.capture.Adopt(rec)
	r.seed(rec.Definition)
	return true
}
