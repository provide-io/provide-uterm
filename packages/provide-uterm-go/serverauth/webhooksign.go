//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"math"
	"strconv"
	"strings"
)

// DefaultMaxAgeS ports webhook_signing._DEFAULT_MAX_AGE_S.
const DefaultMaxAgeS = 300.0

// BuildWebhookSignature ports webhook_signing.build_webhook_signature: returns
// "sha256=<hex>" of HMAC-SHA256 over (timestamp + "." + body), keyed by secret.
// Byte-for-byte compatible with the Python signer.
func BuildWebhookSignature(secret string, body []byte, timestamp string) string {
	signed := make([]byte, 0, len(timestamp)+1+len(body))
	signed = append(signed, timestamp...)
	signed = append(signed, '.')
	signed = append(signed, body...)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(signed)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// VerifyWebhookSignature ports webhook_signing.verify_webhook_signature:
// verify X-Uterm-Signature over ts.body and that the timestamp is fresh. Fails
// closed when the secret is empty (an empty-key HMAC is forgeable). now == nil
// uses the wall clock.
func VerifyWebhookSignature(secret string, body []byte, signatureHeader, timestampHeader string, maxAgeS float64, now *float64) bool {
	if strings.TrimSpace(secret) == "" {
		return false
	}
	if signatureHeader == "" || timestampHeader == "" {
		return false
	}
	tsVal, err := strconv.ParseFloat(timestampHeader, 64)
	if err != nil {
		return false
	}
	current := wallClock()
	if now != nil {
		current = *now
	}
	if math.Abs(current-tsVal) > maxAgeS {
		return false
	}
	supplied := strings.TrimSpace(signatureHeader)
	if strings.HasPrefix(strings.ToLower(supplied), "sha256=") {
		supplied = strings.TrimSpace(supplied[len("sha256="):])
	}
	if supplied == "" {
		return false
	}
	expected := strings.SplitN(BuildWebhookSignature(secret, body, timestampHeader), "=", 2)[1]
	return hmac.Equal([]byte(supplied), []byte(expected))
}
