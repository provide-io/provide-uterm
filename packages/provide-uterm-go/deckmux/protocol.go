//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import "strings"

// DeckMux message-type discriminators (mirror _protocol.py constants and the
// frames package Type* literals for the four wire frames).
const (
	MsgPresenceUpdate      = "presence_update"
	MsgPresenceSync        = "presence_sync"
	MsgPresenceLeave       = "presence_leave"
	MsgControlTransfer     = "control_transfer"
	MsgQueuedInput         = "queued_input"
	MsgControlRequest      = "control_request"
	MsgAutoTransferWarning = "auto_transfer_warning"
)

// Transfer reasons (Python TransferReason literal).
const (
	ReasonHandover      = "handover"
	ReasonAutoIdle      = "auto_idle"
	ReasonAdminTakeover = "admin_takeover"
	ReasonLeaseExpired  = "lease_expired"
)

// Keystroke queue modes (Python KeystrokeQueueMode literal).
const (
	QueueModeDisplay = "display"
	QueueModeReplay  = "replay"
)

// keySymbols maps raw keystroke byte sequences to their UTF-8 display glyphs,
// mirroring _protocol.KEY_SYMBOLS exactly (both 3-char arrow escapes and
// single control chars).
var keySymbols = map[string]string{
	"\x1b[A": "↑", // ↑
	"\x1b[B": "↓", // ↓
	"\x1b[C": "→", // →
	"\x1b[D": "←", // ←
	"\r":     "↵", // ↵
	"\n":     "↵", // ↵
	"\t":     "⇥", // ⇥
	"\x7f":   "⌫", // ⌫
	"\x08":   "⌫", // ⌫
	"\x1b":   "⎋", // ⎋
}

// presenceUpdateOptionalFields is the fixed allow-list of optional keys that
// MakePresenceUpdate copies through — matching the tuple in
// _protocol.make_presence_update.
var presenceUpdateOptionalFields = []string{
	"scroll_line", "scroll_range", "total_lines", "selection",
	"pin", "typing", "queued_keys", "is_owner",
}

// EncodeKeysDisplay converts raw keystroke bytes to their UTF-8 display
// symbols, mirroring _protocol.encode_keys_display: recognised 3-char escape
// sequences and single control chars map to glyphs, other printable runes
// pass through, and unrecognised control chars are dropped.
func EncodeKeysDisplay(rawKeys string) string {
	runes := []rune(rawKeys)
	n := len(runes)
	var b strings.Builder
	i := 0
	for i < n {
		// A 3-char escape sequence needs runes i, i+1, i+2 present, i.e.
		// i+2 < n (strict, matching the Python guard).
		if i+2 < n {
			if sym, ok := keySymbols[string(runes[i:i+3])]; ok {
				b.WriteString(sym)
				i += 3
				continue
			}
		}
		if sym, ok := keySymbols[string(runes[i])]; ok {
			b.WriteString(sym)
			i++
			continue
		}
		if runes[i] >= ' ' { // printable
			b.WriteRune(runes[i])
			i++
			continue
		}
		i++ // skip non-printable
	}
	return b.String()
}

// MakePresenceUpdate builds a presence_update message for broadcast, copying
// only the allow-listed optional fields that are present. Mirrors
// _protocol.make_presence_update.
func MakePresenceUpdate(userID, name, color, role string, fields map[string]any) map[string]any {
	msg := map[string]any{
		"type":    MsgPresenceUpdate,
		"user_id": userID,
		"name":    name,
		"color":   color,
		"role":    role,
	}
	for _, k := range presenceUpdateOptionalFields {
		if v, ok := fields[k]; ok {
			msg[k] = v
		}
	}
	return msg
}

// MakePresenceSync builds a presence_sync message. Mirrors
// _protocol.make_presence_sync.
func MakePresenceSync(users []map[string]any, config map[string]any) map[string]any {
	return map[string]any{
		"type":   MsgPresenceSync,
		"users":  users,
		"config": config,
	}
}

// MakePresenceLeave builds a presence_leave message. Mirrors
// _protocol.make_presence_leave.
func MakePresenceLeave(userID string) map[string]any {
	return map[string]any{
		"type":    MsgPresenceLeave,
		"user_id": userID,
	}
}

// MakeControlTransfer builds a control_transfer message. Mirrors
// _protocol.make_control_transfer (queuedKeys defaults to "" at the call
// site, as in Python's keyword default).
func MakeControlTransfer(fromUser, toUser, reason, queuedKeys string) map[string]any {
	return map[string]any{
		"type":         MsgControlTransfer,
		"from_user_id": fromUser,
		"to_user_id":   toUser,
		"reason":       reason,
		"queued_keys":  queuedKeys,
	}
}
