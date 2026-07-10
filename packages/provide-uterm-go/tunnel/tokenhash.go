//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnel

import (
	"crypto/subtle"
	"encoding/hex"

	"golang.org/x/crypto/blake2b"
)

// HashToken returns the BLAKE2b-256 hex digest of plain.
//
// This is byte-for-byte compatible with the Python
// hashlib.blake2b(plain.encode("utf-8"), digest_size=32).hexdigest() used in
// tunnel/token_hash.py: BLAKE2b with a 32-byte output and no key/salt/person is
// exactly BLAKE2b-256. An empty/absent token hashes to the empty string so
// callers can treat "no token configured" the same as "no match".
func HashToken(plain string) string {
	if plain == "" {
		return ""
	}
	sum := blake2b.Sum256([]byte(plain))
	return hex.EncodeToString(sum[:])
}

// VerifyToken constant-time compares plain's hash against storedHash.
//
// Both the empty-stored-hash case and the empty-plain case return false — a
// configured-but-empty slot must never authenticate any caller. This mirrors
// the Python verify_token (secrets.compare_digest over the hex digests).
func VerifyToken(plain, storedHash string) bool {
	if plain == "" || storedHash == "" {
		return false
	}
	candidate := HashToken(plain)
	return subtle.ConstantTimeCompare([]byte(candidate), []byte(storedHash)) == 1
}
