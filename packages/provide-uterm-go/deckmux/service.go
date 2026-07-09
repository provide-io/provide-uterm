//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"crypto/rand"
	"encoding/hex"
	"sync"
)

// Broadcaster is the host-hub capability DeckMuxPresence needs: deliver a
// DeckMux message to every browser watching a worker. Mirrors the single
// hub.broadcast(worker_id, msg) call the Python service makes.
type Broadcaster interface {
	Broadcast(workerID string, msg map[string]any) error
}

// Conn is a browser connection that exposes a stable, unique per-connection
// anonymous identity. See AnonConn for the canonical implementation.
type Conn interface {
	// DeckMuxAnonID returns a stable per-connection anonymous id. It must
	// never be derived from a pointer/handle the runtime may reuse.
	DeckMuxAnonID() string
}

// Principal is an authenticated identity carrying a stable subject id. It is
// the duck-typed shape the service consumes (Python: getattr(principal,
// "subject_id")). Passing a value that does not implement Principal (or nil)
// takes the anonymous path.
type Principal interface {
	SubjectID() string
}

// DisplayNamed is the optional principal capability exposing a display name
// (Python: getattr(principal, "display_name")).
type DisplayNamed interface {
	DisplayName() string
}

// AnonConn is an embeddable base giving a browser connection a stable, unique
// per-connection anonymous DeckMux identity (a lazily-minted 128-bit random
// token, hex-encoded — the analogue of Python uuid4().hex). The id is never
// derived from a pointer address, so a reconnecting browser can never inherit
// a disconnected one's presence/ownership. Safe for concurrent use.
type AnonConn struct {
	mu sync.Mutex
	id string
}

// DeckMuxAnonID returns the stable anonymous id, minting it on first call.
func (a *AnonConn) DeckMuxAnonID() string {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.id == "" {
		a.id = newAnonID()
	}
	return a.id
}

// newAnonID mints a fresh 128-bit random hex token.
func newAnonID() string {
	var b [16]byte
	_, _ = rand.Read(b[:]) // crypto/rand.Read does not fail on supported platforms
	return hex.EncodeToString(b[:])
}

// DeckMuxPresence is the presence-routing + control-transfer service,
// mirroring _service.DeckMuxPresence. It owns the per-worker PresenceStore and
// TransferManager containers and routes the browser-facing operations. Safe
// for concurrent use: the container maps are mutex-guarded, and each contained
// store/manager is itself concurrency-safe.
type DeckMuxPresence struct {
	mu               sync.Mutex
	hub              Broadcaster
	presenceStores   map[string]*PresenceStore
	transferManagers map[string]*TransferManager
}

// NewDeckMuxPresence constructs the service with a back reference to the host
// hub used for broadcasts.
func NewDeckMuxPresence(hub Broadcaster) *DeckMuxPresence {
	return &DeckMuxPresence{
		hub:              hub,
		presenceStores:   make(map[string]*PresenceStore),
		transferManagers: make(map[string]*TransferManager),
	}
}

// GetPresenceStore returns (creating if needed) the presence store for
// workerID.
func (d *DeckMuxPresence) GetPresenceStore(workerID string) *PresenceStore {
	d.mu.Lock()
	defer d.mu.Unlock()
	store, ok := d.presenceStores[workerID]
	if !ok {
		store = NewPresenceStore()
		d.presenceStores[workerID] = store
	}
	return store
}

// GetTransferManager returns (creating if needed) the transfer manager for
// workerID, applying config (auto_transfer_idle_s, keystroke_queue) only at
// creation time.
func (d *DeckMuxPresence) GetTransferManager(workerID string, config map[string]any) *TransferManager {
	d.mu.Lock()
	defer d.mu.Unlock()
	tm, ok := d.transferManagers[workerID]
	if !ok {
		idle, mode := readTransferConfig(config)
		tm = NewTransferManager(idle, mode)
		d.transferManagers[workerID] = tm
	}
	return tm
}

// OnBrowserConnect registers a connecting browser and returns the presence_sync
// message to send back to it. When other browsers are already present the sync
// is also broadcast so they observe the new user. Mirrors
// _service.on_browser_connect.
func (d *DeckMuxPresence) OnBrowserConnect(workerID string, ws Conn, role string, principal any) (map[string]any, error) {
	store := d.GetPresenceStore(workerID)

	userID := ws.DeckMuxAnonID()
	var name string
	if subject, ok := principalSubject(principal); ok {
		userID = subject
		if dn, ok := principal.(DisplayNamed); ok && dn.DisplayName() != "" {
			name = dn.DisplayName()
		} else {
			name = userID
		}
	} else {
		name = GenerateName(userID)
	}

	color := GenerateColor(userID, store.TakenColors())
	initials := GenerateInitials(name)

	// Prune stale reconnect debris (no activity in the last 30 s).
	store.PruneIdle(30.0)

	store.Add(userID, name, color, role, initials)

	config := map[string]any{"auto_transfer_idle_s": 30, "keystroke_queue": "display"}
	result := store.GetSyncPayload(config)

	// Broadcast to existing browsers so they see the new user (addUser is
	// idempotent on the frontend).
	if store.Count() > 1 {
		if err := d.hub.Broadcast(workerID, result); err != nil {
			return result, err
		}
	}
	return result, nil
}

// OnBrowserDisconnect removes a disconnecting browser and broadcasts a
// presence_leave when the user was present. Mirrors
// _service.on_browser_disconnect.
func (d *DeckMuxPresence) OnBrowserDisconnect(workerID string, ws Conn, principal any) error {
	store := d.GetPresenceStore(workerID)
	userID := d.resolveUserID(ws, principal)
	if _, removed := store.Remove(userID); removed {
		return d.hub.Broadcast(workerID, MakePresenceLeave(userID))
	}
	return nil
}

// HandleMessage routes a DeckMux message from a browser. Mirrors
// _service.handle_message.
func (d *DeckMuxPresence) HandleMessage(workerID string, ws Conn, msg map[string]any, principal any) error {
	msgType, _ := msg["type"].(string)
	store := d.GetPresenceStore(workerID)
	userID := d.resolveUserID(ws, principal)

	switch msgType {
	case MsgPresenceUpdate:
		return d.handlePresenceUpdate(workerID, store, userID, msg)
	case MsgQueuedInput:
		return d.handleQueuedInput(workerID, store, userID, msg)
	case MsgControlRequest:
		return d.handleControlRequest(workerID, store, userID)
	}
	return nil
}

// presenceUpdateInputFields is the fixed allow-list of keys copied from a
// browser presence_update into the store (Python's tuple in handle_message).
var presenceUpdateInputFields = []string{
	"scroll_line", "scroll_range", "total_lines",
	"selection", "pin", "typing", "cols", "rows",
}

func (d *DeckMuxPresence) handlePresenceUpdate(workerID string, store *PresenceStore, userID string, msg map[string]any) error {
	fields := make(map[string]any)
	for _, k := range presenceUpdateInputFields {
		if v, ok := msg[k]; ok {
			fields[k] = v
		}
	}
	user, ok, err := store.Update(userID, fields)
	if err != nil {
		// Malformed/oversized selection/pin — drop the update gracefully (no
		// store mutation, no broadcast), matching the Python ValueError catch.
		return nil
	}
	if !ok {
		return nil
	}
	updateMsg := user.ToDict()
	updateMsg["type"] = MsgPresenceUpdate
	if err := d.hub.Broadcast(workerID, updateMsg); err != nil {
		return err
	}
	// Reset the auto-transfer warning if the active owner is typing.
	if user.IsOwner && asBool(fields["typing"]) {
		d.GetTransferManager(workerID, nil).ResetWarning()
	}
	return nil
}

func (d *DeckMuxPresence) handleQueuedInput(workerID string, store *PresenceStore, userID string, msg map[string]any) error {
	rawKeys, _ := msg["keys"].(string)
	tm := d.GetTransferManager(workerID, nil)
	display := tm.QueueKeystroke(userID, rawKeys)
	// queued_keys is a known, non-validated field, so this update cannot error;
	// it is a no-op when the user is absent (Get below then returns false).
	_, _, _ = store.Update(userID, map[string]any{"queued_keys": display})
	user, ok := store.Get(userID)
	if !ok {
		return nil
	}
	updateMsg := user.ToDict()
	updateMsg["type"] = MsgPresenceUpdate
	return d.hub.Broadcast(workerID, updateMsg)
}

func (d *DeckMuxPresence) handleControlRequest(workerID string, store *PresenceStore, userID string) error {
	owner, hasOwner := store.GetOwner()
	switch {
	case !hasOwner:
		// No one has control — grant immediately.
		store.SetOwner(userID)
		tm := d.GetTransferManager(workerID, nil)
		return d.hub.Broadcast(workerID, tm.BuildTransferMessage("", userID, ReasonHandover))
	case owner.UserID == userID:
		// Requester already owns — release control.
		store.ClearOwner()
		return d.hub.Broadcast(workerID, MakeControlTransfer(userID, "", ReasonHandover, ""))
	}
	// Another user owns — ignore.
	return nil
}

// Cleanup drops all DeckMux state for a worker. Mirrors _service.cleanup.
func (d *DeckMuxPresence) Cleanup(workerID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.presenceStores, workerID)
	delete(d.transferManagers, workerID)
}

// resolveUserID resolves the store key for a connection the same way
// OnBrowserConnect does: the principal's subject id when present, else the
// stable anonymous connection id.
func (d *DeckMuxPresence) resolveUserID(ws Conn, principal any) string {
	if subject, ok := principalSubject(principal); ok {
		return subject
	}
	return ws.DeckMuxAnonID()
}

// principalSubject returns (subject, true) when principal implements Principal.
// A nil or non-Principal value yields ("", false) — the anonymous path,
// mirroring `principal and hasattr(principal, "subject_id")`.
func principalSubject(principal any) (string, bool) {
	if p, ok := principal.(Principal); ok {
		return p.SubjectID(), true
	}
	return "", false
}

// readTransferConfig extracts the transfer-manager settings from a config map,
// applying the Python defaults (idle 30 s, display mode).
func readTransferConfig(config map[string]any) (idleS float64, mode string) {
	idleS = 30
	mode = QueueModeDisplay
	if v, ok := config["auto_transfer_idle_s"]; ok {
		idleS = asFloat(v)
	}
	if v, ok := config["keystroke_queue"].(string); ok {
		mode = v
	}
	return idleS, mode
}
