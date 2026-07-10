//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnel

// Role is the role a tunnel invite grants when consumed.
type Role string

const (
	// RoleViewer is a read-only share participant.
	RoleViewer Role = "viewer"
	// RoleOperator is a read/write (admin-scoped) share participant.
	RoleOperator Role = "operator"
)

// InviteTTLS is the maximum lifetime (seconds) of a one-time invite, clamped
// down further to the owning tunnel's expiry. Matches Python INVITE_TTL_S=300.
const InviteTTLS = 300.0

// TokenRecord is the at-rest tunnel token state stored per tunnel id. It holds
// BLAKE2b digests of the bearer tokens (never the plaintext), matching the
// Python uterm_tunnel_tokens[tunnel_id] shape in routes/tunnels.py. A memory
// disclosure on the server leaks only digests.
type TokenRecord struct {
	WorkerTokenHash  string
	ShareTokenHash   string
	ControlTokenHash string
	CreatedAt        float64
	ExpiresAt        float64
	// IssuedIP is the source IP the tunnel was created from when IP binding is
	// enabled, else nil (issued_ip in Python).
	IssuedIP   *string
	TunnelType string
	SharePage  string
}

// Invite is a consumed one-time invite. It carries the plaintext tunnel token
// the caller should be granted (via a cookie) plus the role and expiry. This is
// the Go analogue of the Python TunnelInvite dataclass.
type Invite struct {
	SessionID   string
	Role        Role
	TunnelToken string
	ExpiresAt   float64
	IssuedIP    *string
}

// inviteRecord is the stored form of an invite, keyed in the store by the
// BLAKE2b hash of the raw invite string (so the raw invite is never persisted).
type inviteRecord struct {
	sessionID   string
	role        Role
	tunnelToken string
	expiresAt   float64
	issuedIP    *string
}

// InviteMatchesTokenHash reports whether a consumed invite still matches the
// active tunnel token digest. Mirrors tunnel_invite_matches_token_hash.
func InviteMatchesTokenHash(invite *Invite, tokenHash string) bool {
	if invite == nil {
		return false
	}
	return VerifyToken(invite.TunnelToken, tokenHash)
}
