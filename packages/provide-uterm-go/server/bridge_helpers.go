//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"crypto/rand"
	"encoding/hex"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/hub"
)

// newHijackID returns a UUIDv4-format id (matching the ^[0-9a-f\-]{1,64}$
// path constraint), replacing the Python uuid4() lease-id source.
func newHijackID() string {
	var b [16]byte
	// crypto/rand.Read does not fail on supported platforms.
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	s := hex.EncodeToString(b[:])
	return s[0:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:32]
}

// hubClampLease clamps a lease to the hub's accepted range.
func hubClampLease(leaseS int) int { return hub.ClampLease(leaseS) }

// controlMsg builds a worker control frame ({"type":"control","action":...}).
func controlMsg(action, owner string, leaseS int, ts float64, hijackID string) map[string]any {
	m := map[string]any{
		"type":    "control",
		"action":  action,
		"owner":   owner,
		"lease_s": leaseS,
		"ts":      ts,
	}
	if hijackID != "" {
		m["hijack_id"] = hijackID
	}
	return m
}

// acquireErrorMessage maps a try_acquire error kind to its client message.
func acquireErrorMessage(kind string) string {
	switch kind {
	case "no_worker":
		return "No worker connected for this session."
	case "already_hijacked":
		return "Worker is already hijacked."
	case "open_mode":
		return "Hijack not available in open input mode."
	default:
		return kind
	}
}

// extractPromptID pulls prompt_detected.prompt_id from a snapshot dict.
func extractPromptID(snapshot map[string]any) any {
	if snapshot == nil {
		return nil
	}
	prompt, ok := snapshot["prompt_detected"].(map[string]any)
	if !ok {
		return nil
	}
	if v, ok := prompt["prompt_id"].(string); ok && v != "" {
		return v
	}
	return nil
}

// intField reads an int-ish JSON field with a default.
func intField(m map[string]any, k string, def int) int {
	if m == nil {
		return def
	}
	switch v := m[k].(type) {
	case float64:
		return int(v)
	case int:
		return v
	default:
		return def
	}
}
