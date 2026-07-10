//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func principal(subject string, roles ...string) *Principal {
	if len(roles) == 0 {
		roles = []string{"operator"}
	}
	return &Principal{SubjectID: subject, Roles: NewSet(roles...), Scopes: NewSet()}
}

func session(id string, owner string, visibility string) *serverconfig.SessionDefinition {
	sd := &serverconfig.SessionDefinition{SessionID: id, DisplayName: "Test", ConnectorType: "shell", Visibility: visibility}
	if owner != "" {
		sd.Owner = &owner
	}
	return sd
}

func TestCapabilitiesFor(t *testing.T) {
	authz := NewAuthorizationService()
	if caps := authz.CapabilitiesFor(principal("u", "viewer")); !caps.Has("session.read") || caps.Has("session.control.create") {
		t.Errorf("viewer caps wrong: %v", caps.Sorted())
	}
	if caps := authz.CapabilitiesFor(principal("u", "operator")); !caps.Has("session.control.create") || caps.Has("session.control.delete") {
		t.Errorf("operator caps wrong: %v", caps.Sorted())
	}
	if caps := authz.CapabilitiesFor(principal("u", "admin")); !caps.Has("session.control.delete") || !caps.Has("session.control.hijack") {
		t.Errorf("admin caps wrong: %v", caps.Sorted())
	}
	if caps := authz.CapabilitiesFor(principal("u", "unknown_role")); len(caps) != 0 {
		t.Errorf("unknown role caps = %v", caps.Sorted())
	}
}

func TestScopeNarrowing(t *testing.T) {
	authz := NewAuthorizationService()
	// wildcard = unrestricted
	wild := &Principal{SubjectID: "u", Roles: NewSet("admin"), Scopes: NewSet("*")}
	if !authz.CapabilitiesFor(wild).Has("session.control.delete") {
		t.Errorf("wildcard scope narrowed caps")
	}
	// empty scopes = unrestricted
	empty := &Principal{SubjectID: "u", Roles: NewSet("admin"), Scopes: NewSet()}
	if !authz.CapabilitiesFor(empty).Has("session.control.delete") {
		t.Errorf("empty scopes narrowed caps")
	}
	// narrowing scope
	narrow := &Principal{SubjectID: "u", Roles: NewSet("admin"), Scopes: NewSet("session.read")}
	caps := authz.CapabilitiesFor(narrow)
	if len(caps) != 1 || !caps.Has("session.read") {
		t.Errorf("narrowed caps = %v", caps.Sorted())
	}
	// scope cannot grant beyond role
	beyond := &Principal{SubjectID: "u", Roles: NewSet("viewer"), Scopes: NewSet("session.control.delete")}
	c := authz.CapabilitiesFor(beyond)
	if c.Has("session.control.delete") || c.Has("session.read") {
		t.Errorf("scope granted beyond role: %v", c.Sorted())
	}
}

func TestCanReadSession(t *testing.T) {
	authz := NewAuthorizationService()
	if !authz.CanReadSession(principal("u", "viewer"), session("s", "", "public")) {
		t.Errorf("viewer cannot read public")
	}
	if authz.CanReadSession(principal("u", "unknown_role"), session("s", "", "public")) {
		t.Errorf("no-cap role read public")
	}
	if !authz.CanReadSession(principal("u", "admin"), session("s", "someone", "private")) {
		t.Errorf("admin cannot read private")
	}
	if !authz.CanReadSession(principal("alice", "operator"), session("s", "alice", "private")) {
		t.Errorf("owner cannot read own private")
	}
	if !authz.CanReadSession(principal("u", "operator"), session("s", "", "operator")) {
		t.Errorf("operator cannot read operator-visibility")
	}
	if authz.CanReadSession(principal("u", "viewer"), session("s", "", "operator")) {
		t.Errorf("viewer read operator-visibility")
	}
	if authz.CanReadSession(principal("bob", "viewer"), session("s", "alice", "private")) {
		t.Errorf("viewer read others private")
	}
	// share-token principal
	if !authz.CanReadSession(principal("share:tunnel-abc:viewer", "viewer"), session("tunnel-abc", "alice", "private")) {
		t.Errorf("share principal cannot read its tunnel")
	}
	if authz.CanReadSession(principal("share:tunnel-abc:viewer", "viewer"), session("tunnel-xyz", "alice", "private")) {
		t.Errorf("share principal read other tunnel")
	}
}

func TestCanMutateSession(t *testing.T) {
	authz := NewAuthorizationService()
	if !authz.CanMutateSession(principal("u", "admin"), session("s", "", "public"), "session.control.update") {
		t.Errorf("admin cannot mutate ownerless")
	}
	if authz.CanMutateSession(principal("u", "operator"), session("s", "", "public"), "session.control.update") {
		t.Errorf("operator mutated ownerless system session")
	}
	if !authz.CanMutateSession(principal("alice", "operator"), session("s", "alice", "public"), "session.control.update") {
		t.Errorf("operator cannot mutate owned")
	}
	if authz.CanMutateSession(principal("bob", "operator"), session("s", "alice", "public"), "session.control.update") {
		t.Errorf("operator mutated others session")
	}
	if authz.CanMutateSession(principal("u", "operator"), session("s", "operator_user", "public"), "session.control.delete") {
		t.Errorf("operator got delete capability")
	}
}

func TestSessionScopedShareOperator(t *testing.T) {
	authz := NewAuthorizationService()
	scoped := func(id string) *Principal {
		return &Principal{SubjectID: "share:" + id + ":operator", Roles: NewSet("admin"), Scopes: NewSet("*"), AdminSessionScope: &id}
	}
	if authz.IsAdmin(scoped("A")) {
		t.Errorf("scoped operator is a global admin")
	}
	if !authz.CanMutateSession(scoped("A"), session("A", "", "public"), "session.control.hijack") {
		t.Errorf("scoped operator cannot mutate own session")
	}
	if authz.CanMutateSession(scoped("A"), session("B", "", "public"), "session.control.hijack") {
		t.Errorf("scoped operator mutated other session")
	}
	if !authz.CanReadSession(scoped("A"), session("A", "", "private")) {
		t.Errorf("scoped operator cannot read own private session")
	}
	if authz.CanReadSession(scoped("A"), session("B", "", "private")) {
		t.Errorf("scoped operator read other private session")
	}
}

func TestResolveBrowserRole(t *testing.T) {
	authz := NewAuthorizationService()
	if r := authz.ResolveBrowserRole(principal("u", "admin"), session("s", "", "public")); r != "admin" {
		t.Errorf("admin role = %q", r)
	}
	if r := authz.ResolveBrowserRole(principal("op", "operator"), session("s", "", "public")); r != "operator" {
		t.Errorf("operator role = %q", r)
	}
	if r := authz.ResolveBrowserRole(principal("u", "viewer"), session("s", "", "public")); r != "viewer" {
		t.Errorf("viewer role = %q", r)
	}
	if r := authz.ResolveBrowserRole(principal("u", "unknown"), session("s", "", "public")); r != "viewer" {
		t.Errorf("no-read role = %q", r)
	}
}

func TestProfileAuthorization(t *testing.T) {
	authz := NewAuthorizationService()
	prof := func(owner, visibility string) *serverconfig.ConnectionProfile {
		return &serverconfig.ConnectionProfile{ProfileID: "p", Owner: owner, Name: "T", ConnectorType: "ssh", Visibility: visibility}
	}
	alice := principal("alice", "operator")
	if !authz.CanReadProfile(alice, prof("alice", "private")) {
		t.Errorf("owner cannot read own private profile")
	}
	if authz.CanReadProfile(alice, prof("bob", "private")) {
		t.Errorf("read others private profile")
	}
	if !authz.CanReadProfile(alice, prof("bob", "shared")) {
		t.Errorf("cannot read shared profile")
	}
	admin := principal("admin", "admin")
	if !authz.CanReadProfile(admin, prof("bob", "private")) {
		t.Errorf("admin cannot read any profile")
	}
	if !authz.CanMutateProfile(alice, prof("alice", "private")) {
		t.Errorf("owner cannot mutate own profile")
	}
	if authz.CanMutateProfile(alice, prof("bob", "private")) {
		t.Errorf("mutated others profile")
	}
	if !authz.CanMutateProfile(admin, prof("bob", "private")) {
		t.Errorf("admin cannot mutate any profile")
	}
}

func TestHasRoleAndRecording(t *testing.T) {
	authz := NewAuthorizationService()
	if !authz.HasRole(principal("u", "operator"), "operator") {
		t.Errorf("has_role failed")
	}
	if !authz.CanReadRecording(principal("u", "viewer"), session("s", "", "public")) {
		t.Errorf("viewer cannot read public recording")
	}
	if !authz.CanCreateSession(principal("u", "operator")) {
		t.Errorf("operator cannot create session")
	}
}
