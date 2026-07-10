//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnel

import (
	"crypto/rand"
	"encoding/base64"
)

// tokenBytes is the entropy of a generated bearer token. 32 bytes (256 bits)
// matches the Python secrets.token_urlsafe(32) used to mint worker/share/
// control tokens and one-time invites.
const tokenBytes = 32

// GenerateToken returns a URL-safe, base64 (no padding) bearer token backed by
// 32 bytes of crypto/rand entropy. This is the shape of Python's
// secrets.token_urlsafe(32) — the raw value need not match Python byte-for-byte
// (both are random), only the HashToken scheme must, so a token minted on
// either side validates on the other.
func GenerateToken() string {
	b := make([]byte, tokenBytes)
	// crypto/rand.Read never returns an error on supported platforms; the value
	// only needs to be unpredictable. This matches the convention used by the
	// server package (randHex / newRequestID).
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}
