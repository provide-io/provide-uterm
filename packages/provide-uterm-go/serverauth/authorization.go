//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"strings"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// RoleCapabilities ports authorization.ROLE_CAPABILITIES — the RBAC map of
// role → granted capability set.
var RoleCapabilities = map[string]Set{
	"viewer": NewSet("session.read", "session.recording.read", "graphical.target.read", "graphical.session.attach"),
	"operator": NewSet(
		"session.read", "session.recording.read",
		"session.control.create", "session.control.connect",
		"session.control.mode", "session.control.clear", "session.control.update",
		"graphical.target.read", "graphical.session.attach",
	),
	"admin": NewSet(
		"session.read", "session.recording.read",
		"session.control.create", "session.control.connect",
		"session.control.mode", "session.control.clear", "session.control.update",
		"session.control.delete", "session.control.hijack",
		"graphical.target.read", "graphical.target.manage", "graphical.session.attach",
	),
}

// AuthorizationProvider ports authorization.AuthorizationProvider — the
// pluggable authorization decision surface.
type AuthorizationProvider interface {
	CapabilitiesFor(p *Principal) Set
	HasCapability(p *Principal, capability string) bool
	IsAdmin(p *Principal) bool
	IsOwner(p *Principal, session *serverconfig.SessionDefinition) bool
	CanReadSession(p *Principal, session *serverconfig.SessionDefinition) bool
	CanReadRecording(p *Principal, session *serverconfig.SessionDefinition) bool
	CanCreateSession(p *Principal) bool
	CanMutateSession(p *Principal, session *serverconfig.SessionDefinition, action string) bool
	CanReadProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool
	CanMutateProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool
	ResolveBrowserRole(p *Principal, session *serverconfig.SessionDefinition) string
}

// LocalAuthorizationProvider ports authorization.LocalAuthorizationProvider —
// the standard RBAC implementation.
type LocalAuthorizationProvider struct{}

// CapabilitiesFor ports LocalAuthorizationProvider.capabilities_for.
func (LocalAuthorizationProvider) CapabilitiesFor(p *Principal) Set {
	if p == nil {
		return NewSet()
	}
	roleCaps := NewSet()
	for role := range p.Roles {
		for cap := range RoleCapabilities[role] {
			roleCaps[cap] = struct{}{}
		}
	}
	if len(p.Scopes) > 0 && !p.Scopes.Has("*") {
		narrowed := NewSet()
		for cap := range roleCaps {
			if p.Scopes.Has(cap) {
				narrowed[cap] = struct{}{}
			}
		}
		return narrowed
	}
	return roleCaps
}

// HasCapability ports has_capability.
func (l LocalAuthorizationProvider) HasCapability(p *Principal, capability string) bool {
	if strings.HasPrefix(capability, "graphical.") && (p == nil || p.TenantID == "") {
		return false
	}
	return l.CapabilitiesFor(p).Has(capability)
}

// IsAdmin ports is_admin: a session-scoped admin grant is NOT a global admin.
func (LocalAuthorizationProvider) IsAdmin(p *Principal) bool {
	return p.Roles.Has("admin") && p.AdminSessionScope == nil
}

// isAdminForSession ports _is_admin_for_session.
func isAdminForSession(p *Principal, session *serverconfig.SessionDefinition) bool {
	if !p.Roles.Has("admin") {
		return false
	}
	return p.AdminSessionScope == nil || *p.AdminSessionScope == session.SessionID
}

// IsOwner ports is_owner.
func (LocalAuthorizationProvider) IsOwner(p *Principal, session *serverconfig.SessionDefinition) bool {
	return session.Owner != nil && *session.Owner == p.SubjectID
}

// CanReadSession ports can_read_session.
func (l LocalAuthorizationProvider) CanReadSession(p *Principal, session *serverconfig.SessionDefinition) bool {
	if !l.HasCapability(p, "session.read") {
		return false
	}
	if isAdminForSession(p, session) || l.IsOwner(p, session) {
		return true
	}
	if strings.HasPrefix(p.SubjectID, "share:"+session.SessionID+":") {
		return true
	}
	switch session.Visibility {
	case "public":
		return true
	case "operator":
		return p.Roles.Has("operator")
	}
	return false
}

// CanReadRecording ports can_read_recording.
func (l LocalAuthorizationProvider) CanReadRecording(p *Principal, session *serverconfig.SessionDefinition) bool {
	return l.CanReadSession(p, session) && l.HasCapability(p, "session.recording.read")
}

// CanCreateSession ports can_create_session.
func (l LocalAuthorizationProvider) CanCreateSession(p *Principal) bool {
	return l.HasCapability(p, "session.control.create")
}

// CanMutateSession ports can_mutate_session.
func (l LocalAuthorizationProvider) CanMutateSession(p *Principal, session *serverconfig.SessionDefinition, action string) bool {
	if !l.HasCapability(p, action) {
		return false
	}
	if isAdminForSession(p, session) {
		return true
	}
	if session.Owner == nil {
		return false
	}
	return l.IsOwner(p, session)
}

// CanReadProfile ports can_read_profile.
func (l LocalAuthorizationProvider) CanReadProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool {
	return profile.Owner == p.SubjectID || profile.Visibility == "shared" || l.IsAdmin(p)
}

// CanMutateProfile ports can_mutate_profile.
func (l LocalAuthorizationProvider) CanMutateProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool {
	return profile.Owner == p.SubjectID || l.IsAdmin(p)
}

// ResolveBrowserRole ports resolve_browser_role.
func (l LocalAuthorizationProvider) ResolveBrowserRole(p *Principal, session *serverconfig.SessionDefinition) string {
	if !l.CanReadSession(p, session) {
		return "viewer"
	}
	if l.CanMutateSession(p, session, "session.control.hijack") {
		return "admin"
	}
	if p.Roles.Has("operator") || l.IsOwner(p, session) {
		return "operator"
	}
	return "viewer"
}

// AuthorizationService ports authorization.AuthorizationService — the
// pluggable gateway. It delegates to a provider (default: local RBAC).
type AuthorizationService struct {
	provider AuthorizationProvider
}

// NewAuthorizationService builds a service backed by the local RBAC provider.
func NewAuthorizationService() *AuthorizationService {
	return &AuthorizationService{provider: LocalAuthorizationProvider{}}
}

// NewAuthorizationServiceWith builds a service backed by a custom provider.
func NewAuthorizationServiceWith(provider AuthorizationProvider) *AuthorizationService {
	return &AuthorizationService{provider: provider}
}

// HasRole ports has_role — a direct role-membership check (never delegated).
func (s *AuthorizationService) HasRole(p *Principal, role string) bool { return p.Roles.Has(role) }

// CapabilitiesFor delegates.
func (s *AuthorizationService) CapabilitiesFor(p *Principal) Set {
	return s.provider.CapabilitiesFor(p)
}

// HasCapability delegates.
func (s *AuthorizationService) HasCapability(p *Principal, capability string) bool {
	return s.provider.HasCapability(p, capability)
}

// IsAdmin delegates.
func (s *AuthorizationService) IsAdmin(p *Principal) bool { return s.provider.IsAdmin(p) }

// IsOwner delegates.
func (s *AuthorizationService) IsOwner(p *Principal, session *serverconfig.SessionDefinition) bool {
	return s.provider.IsOwner(p, session)
}

// CanReadSession delegates.
func (s *AuthorizationService) CanReadSession(p *Principal, session *serverconfig.SessionDefinition) bool {
	return s.provider.CanReadSession(p, session)
}

// CanReadRecording delegates.
func (s *AuthorizationService) CanReadRecording(p *Principal, session *serverconfig.SessionDefinition) bool {
	return s.provider.CanReadRecording(p, session)
}

// CanCreateSession delegates.
func (s *AuthorizationService) CanCreateSession(p *Principal) bool {
	return s.provider.CanCreateSession(p)
}

// CanMutateSession delegates.
func (s *AuthorizationService) CanMutateSession(p *Principal, session *serverconfig.SessionDefinition, action string) bool {
	return s.provider.CanMutateSession(p, session, action)
}

// CanReadProfile delegates.
func (s *AuthorizationService) CanReadProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool {
	return s.provider.CanReadProfile(p, profile)
}

// CanMutateProfile delegates.
func (s *AuthorizationService) CanMutateProfile(p *Principal, profile *serverconfig.ConnectionProfile) bool {
	return s.provider.CanMutateProfile(p, profile)
}

// ResolveBrowserRole delegates. This is the SessionPolicyResolver.role_for
// entrypoint (policy.py wraps exactly this call).
func (s *AuthorizationService) ResolveBrowserRole(p *Principal, session *serverconfig.SessionDefinition) string {
	return s.provider.ResolveBrowserRole(p, session)
}
