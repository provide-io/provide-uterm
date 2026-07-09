//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"math"
	"sync"
)

// MaxQueueLength caps the per-user raw keystroke buffer, mirroring
// _transfer.MAX_QUEUE_LENGTH.
const MaxQueueLength = 256

// TransferManager implements control-transfer logic and the non-owner
// keystroke queue, mirroring _transfer.TransferManager. It does not own the
// input lease; it only computes transfer decisions and payloads. Safe for
// concurrent use.
type TransferManager struct {
	mu          sync.Mutex
	autoIdleS   float64
	queueMode   string
	queues      map[string]string // user id -> raw key buffer
	warningSent bool
}

// NewTransferManager constructs a manager. An autoIdleS <= 0 disables
// auto-transfer; queueMode is QueueModeDisplay or QueueModeReplay (an empty
// mode defaults to display, matching the Python keyword default).
func NewTransferManager(autoIdleS float64, queueMode string) *TransferManager {
	if queueMode == "" {
		queueMode = QueueModeDisplay
	}
	return &TransferManager{
		autoIdleS: autoIdleS,
		queueMode: queueMode,
		queues:    make(map[string]string),
	}
}

// AutoTransferEnabled reports whether auto-transfer on idle is enabled.
func (t *TransferManager) AutoTransferEnabled() bool {
	return t.autoIdleS > 0
}

// QueueMode returns the current keystroke queue mode.
func (t *TransferManager) QueueMode() string {
	return t.queueMode
}

// QueueKeystroke buffers keystrokes for a non-owner, truncating the raw buffer
// to the last MaxQueueLength runes, and returns the display string.
func (t *TransferManager) QueueKeystroke(userID, rawKeys string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	buf := t.queues[userID] + rawKeys
	if r := []rune(buf); len(r) > MaxQueueLength {
		buf = string(r[len(r)-MaxQueueLength:])
	}
	t.queues[userID] = buf
	return EncodeKeysDisplay(buf)
}

// FlushQueue removes and returns the raw keystroke buffer for a user.
func (t *TransferManager) FlushQueue(userID string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	raw := t.queues[userID]
	delete(t.queues, userID)
	return raw
}

// ClearQueue removes a user's keystroke buffer without returning it.
func (t *TransferManager) ClearQueue(userID string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	delete(t.queues, userID)
}

// GetQueueDisplay returns the display-format keystroke queue for a user ("" if
// empty).
func (t *TransferManager) GetQueueDisplay(userID string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	raw := t.queues[userID]
	if raw == "" {
		return ""
	}
	return EncodeKeysDisplay(raw)
}

// CheckAutoTransfer decides whether to warn or transfer given the owner's idle
// seconds and the set of queued users. Returns (shouldWarn, shouldTransfer).
// Mirrors _transfer.check_auto_transfer, including the warning latch.
func (t *TransferManager) CheckAutoTransfer(ownerIdleS float64, queuedUsers []string) (shouldWarn, shouldTransfer bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.autoIdleS <= 0 || len(queuedUsers) == 0 {
		t.warningSent = false
		return false, false
	}
	warnThreshold := math.Max(0, t.autoIdleS-10)
	if ownerIdleS >= t.autoIdleS {
		t.warningSent = false
		return false, true
	}
	if ownerIdleS >= warnThreshold && !t.warningSent {
		t.warningSent = true
		return true, false
	}
	return false, false
}

// ResetWarning clears the warning-sent latch (call when the owner is active).
func (t *TransferManager) ResetWarning() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.warningSent = false
}

// BuildTransferMessage builds a control_transfer message, draining the target
// user's keystroke queue per the queue mode: replay mode flushes the raw
// bytes; display mode emits the encoded display and clears the queue.
func (t *TransferManager) BuildTransferMessage(fromUser, toUser, reason string) map[string]any {
	var queued string
	if t.queueMode == QueueModeReplay {
		queued = t.FlushQueue(toUser)
	} else {
		queued = t.GetQueueDisplay(toUser)
		t.ClearQueue(toUser)
	}
	return MakeControlTransfer(fromUser, toUser, reason, queued)
}
