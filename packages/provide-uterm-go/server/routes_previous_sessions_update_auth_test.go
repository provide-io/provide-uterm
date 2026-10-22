//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"reflect"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// Who may change a previous session's owner, and the order the request is
// judged in. Port of the matching tests in test_previous_sessions.py and
// test_previous_sessions_update.py.

func scoped(subject string, scopes ...string) map[string]string {
	return map[string]string{"X-Subject": subject, "X-Role": "admin", "X-Scopes": strings.Join(scopes, ",")}
}

// An admin token whose scopes leave it only session.read may not read bob's
// private history, so it may not make itself its owner and then read it.
func TestAnAdminTokenNarrowedToReadingCannotTakeASessionOver(t *testing.T) {
	ts, dir := previousServer(t, 7)
	headers := scoped("eve", "session.read")
	before := onDisk(t, dir, bobPrivate)
	if rec := ts.do("GET", "/api/previous-sessions/"+bobPrivate, "", headers); rec.Code != http.StatusForbidden {
		t.Fatalf("read: %d", rec.Code)
	}
	code, out := patchPrevious(ts, bobPrivate, `{"owner":"eve"}`, headers)
	if code != http.StatusForbidden || out["detail"] != "admin privileges required" {
		t.Fatalf("patch: %d %v", code, out)
	}
	if !reflect.DeepEqual(onDisk(t, dir, bobPrivate), before) {
		t.Error("the file changed")
	}
	if rec := ts.do("GET", "/api/previous-sessions/"+bobPrivate, "", headers); rec.Code != http.StatusForbidden {
		t.Fatalf("read afterwards: %d", rec.Code)
	}
}

func TestAnAdminNeedsTakeOverAndUpdateInItsScopes(t *testing.T) {
	for _, tc := range []struct {
		scopes []string
		status int
	}{
		// Take-over without the live PATCH's capability, and the reverse.
		{[]string{"session.control.hijack"}, http.StatusForbidden},
		{[]string{"session.control.update"}, http.StatusForbidden},
		{[]string{"graphical.target.read"}, http.StatusForbidden},
		{[]string{"session.control.hijack", "session.control.update"}, http.StatusOK},
		{[]string{"*"}, http.StatusOK},
	} {
		ts, _ := previousServer(t, 7)
		if code, _ := patchPrevious(ts, bobPrivate, `{"owner":"blackbetty:7"}`, scoped("root", tc.scopes...)); code != tc.status {
			t.Errorf("%v: %d, want %d", tc.scopes, code, tc.status)
		}
	}
}

func TestAnAdminOfAnotherSessionIsRefusedAndNothingChanges(t *testing.T) {
	ts, dir := previousServer(t, 7)
	before := onDisk(t, dir, alicePrivate)
	headers := map[string]string{"X-Subject": "scoped", "X-Role": "admin", "X-Admin-Session-Scope": bobPrivate}
	if code, _ := patchPrevious(ts, alicePrivate, `{"owner":"scoped"}`, headers); code != http.StatusForbidden {
		t.Fatalf("%d", code)
	}
	if !reflect.DeepEqual(onDisk(t, dir, alicePrivate), before) {
		t.Error("the file changed")
	}
}

// Black Betty's gateway token is an admin with no scope claim at all: every
// capability its roles give.
func TestAnUnscopedAdminTokenLikeBlackBettysIsAdmitted(t *testing.T) {
	ts, _ := previousServer(t, 7)
	code, out := patchPrevious(ts, bobPrivate, `{"owner":"blackbetty:7","visibility":"private"}`,
		map[string]string{"X-Subject": "blackbetty-gateway", "X-Role": "admin"})
	if code != http.StatusOK || out["owner"] != "blackbetty:7" {
		t.Fatalf("%d %v", code, out)
	}
}

// policyMutator is policyRoles whose live-PATCH rule admits "policy-admin"
// alone, recording what it is asked.
type policyMutator struct {
	policyRoles
	asked *[]string
}

func (m policyMutator) CanMutateSession(p *serverauth.Principal, session *serverconfig.SessionDefinition, action string) bool {
	*m.asked = append(*m.asked, p.SubjectID+"|"+session.SessionID+"|"+action)
	return p.SubjectID == "policy-admin"
}

func TestTheAdminIsTheOneTheProviderNames(t *testing.T) {
	var asked []string
	ts, _ := previousServer(t, 7, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Authz = serverauth.NewAuthorizationServiceWith(policyMutator{asked: &asked})
	})
	if code, _ := patchPrevious(ts, alicePrivate, `{"owner":"x"}`, rootAdmin); code != http.StatusForbidden {
		t.Fatalf("a role the provider withholds: %d", code)
	}
	if code, _ := patchPrevious(ts, alicePrivate, `{"owner":"x"}`, as("policy-admin", "viewer")); code != http.StatusOK {
		t.Fatalf("the provider's admin: %d", code)
	}
	// The live PATCH's own question, about the definition the session ended with.
	if want := "policy-admin|" + alicePrivate + "|session.control.update"; !reflect.DeepEqual(asked, []string{want}) {
		t.Fatalf("asked %v, want %s", asked, want)
	}
}

// refusesMutation is a provider whose live-PATCH rule refuses everyone, admins
// included.
type refusesMutation struct {
	serverauth.LocalAuthorizationProvider
}

func (refusesMutation) CanMutateSession(*serverauth.Principal, *serverconfig.SessionDefinition, string) bool {
	return false
}

func TestAnAdminTheLivePatchWouldRefuseIsRefused(t *testing.T) {
	ts, dir := previousServer(t, 7, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Authz = serverauth.NewAuthorizationServiceWith(refusesMutation{})
	})
	before := onDisk(t, dir, bobPrivate)
	code, out := patchPrevious(ts, bobPrivate, `{"owner":"eve"}`, rootAdmin)
	if code != http.StatusForbidden || out["detail"] != "admin privileges required" {
		t.Fatalf("%d %v", code, out)
	}
	if !reflect.DeepEqual(onDisk(t, dir, bobPrivate), before) {
		t.Error("the file changed")
	}
}

// Bodies no object can be read from: none at all, malformed JSON, bytes that
// are not UTF-8, JSON that is not an object, and an object with more after it.
var unreadableBodies = map[string]string{
	"none": "", "malformed": "{not json", "not utf-8": "\xff\xfe", "list": "[1]", "string": `"owner"`,
	"null": "null", "number": "7", "trailing junk": `{"owner":"x"} junk`, "two objects": `{"owner":"x"}{"owner":"y"}`,
	"bad utf-8 in a string": "{\"owner\":\"\xff\"}",
}

func TestABodyThatIsNotAnObjectIs422(t *testing.T) {
	ts, dir := previousServer(t, 7)
	before := onDisk(t, dir, alicePrivate)
	for name, body := range unreadableBodies {
		code, out := patchPrevious(ts, alicePrivate, body, rootAdmin)
		if code != http.StatusUnprocessableEntity || out["detail"] != "invalid request body" {
			t.Errorf("%s: %d %v", name, code, out)
		}
	}
	if !reflect.DeepEqual(onDisk(t, dir, alicePrivate), before) {
		t.Error("the file changed")
	}
}

// 401, then 404, then 403, then 422, whatever the body: a caller learns
// nothing about the body's handling it could not learn without one.
func TestABodyIsJudgedOnlyAfterTheCallerAndTheSession(t *testing.T) {
	bodies := map[string]string{"colour": `{"colour":"red"}`}
	for name, body := range unreadableBodies {
		bodies[name] = body
	}
	ts, _ := previousServer(t, 7)
	for name, body := range bodies {
		if code, _ := patchPrevious(ts, alicePrivate, body, nil); code != http.StatusUnauthorized {
			t.Errorf("%s anonymous: %d", name, code)
		}
		if code, _ := patchPrevious(ts, "pam-alice-capture-9-9", body, rootAdmin); code != http.StatusNotFound {
			t.Errorf("%s unknown: %d", name, code)
		}
		code, out := patchPrevious(ts, alicePrivate, body, as("alice", "viewer"))
		if code != http.StatusForbidden || out["detail"] != "admin privileges required" {
			t.Errorf("%s non-admin: %d %v", name, code, out)
		}
	}
}
