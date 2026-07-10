//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package conformance

import (
	"crypto/hmac"
	"crypto/sha256"
)

// hmacSHA256 returns HMAC-SHA256(key, msg). Kept local so the conformance
// suite does not import the full server graph just to check the webhook
// signature format.
func hmacSHA256(key, msg []byte) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(msg)
	return mac.Sum(nil)
}
