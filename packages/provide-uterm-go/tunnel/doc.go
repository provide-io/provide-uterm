//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package tunnel ports the provide-uterm tunnel invite/token lifecycle to Go.
//
// It provides:
//
//   - Secure token generation (GenerateToken, crypto/rand) matching the
//     Python secrets.token_urlsafe(32) shape (32 random bytes, base64url,
//     no padding).
//   - At-rest token hashing (HashToken / VerifyToken) that is byte-for-byte
//     compatible with the Python tunnel/token_hash.py BLAKE2b-256 scheme, so a
//     token issued by the Python server validates in Go and vice versa.
//   - One-time viewer/operator invites with a TTL/expiry (InviteTTLS) clamped
//     to the owning tunnel's expiry, single-use consumption semantics, and a
//     store interface (Store) with a concurrency-safe in-memory implementation
//     (MemStore).
//
// The design mirrors the Python modules tunnel_invites.py (invite issuance /
// consumption / discard / sweep) and routes/tunnels.py (token record storage)
// so the HTTP surface in the server package can be a faithful port.
package tunnel
