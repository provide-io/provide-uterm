//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"os/user"
	"strconv"
	"testing"
)

func intPtr(i int) *int { return &i }

// currentUser returns the OS test-runner's own identity for tests that resolve
// "myself" and expect success. Skips (not fails) when running as root: the
// privileged-uid/gid guard in Resolve is intentional security hardening, so a
// root test runner (some containerized dev/CI setups; never GitHub-hosted
// ubuntu-latest, which runs as the non-root "runner" user) would otherwise
// fail these tests against correct, deliberate behavior rather than a bug.
func currentUser(t *testing.T) (u *user.User, uid, gid int) {
	t.Helper()
	u, err := user.Current()
	if err != nil {
		t.Skipf("user.Current unavailable: %v", err)
	}
	uid, _ = strconv.Atoi(u.Uid)
	gid, _ = strconv.Atoi(u.Gid)
	if uid == 0 {
		t.Skip("running as root — privileged-uid resolution is intentionally rejected")
	}
	return u, uid, gid
}

func TestResolveDefaultCurrentUser(t *testing.T) {
	u, uid, gid := currentUser(t)
	r, err := NewUidMap(nil, false).Resolve(u.Username, ResolveOpts{})
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if r.UID != uid || r.GID != gid || r.Name != u.Username || r.Home != u.HomeDir {
		t.Fatalf("mismatch: %+v (want uid=%d gid=%d name=%s home=%s)", r, uid, gid, u.Username, u.HomeDir)
	}
	if r.Shell == "" {
		t.Fatalf("shell should be non-empty")
	}
}

func TestResolveUnknownUsername(t *testing.T) {
	_, err := NewUidMap(nil, false).Resolve("__no_such_user_xyzzy__", ResolveOpts{})
	assertErr(t, err, "no such OS user")
	if !IsUidMapError(err) {
		t.Fatalf("expected UidMapError, got %T", err)
	}
}

func TestResolveRunAsUIDOverride(t *testing.T) {
	_, uid, _ := currentUser(t)
	r, err := NewUidMap(nil, false).Resolve("anything", ResolveOpts{RunAsUID: intPtr(uid)})
	if err != nil || r.UID != uid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveRunAsUIDExplicitGID(t *testing.T) {
	_, uid, _ := currentUser(t)
	r, err := NewUidMap(nil, true).Resolve("anything", ResolveOpts{RunAsUID: intPtr(uid), RunAsGID: intPtr(0)})
	if err != nil || r.UID != uid || r.GID != 0 {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveRunAsName(t *testing.T) {
	u, uid, _ := currentUser(t)
	r, err := NewUidMap(nil, false).Resolve("anything", ResolveOpts{RunAs: u.Username})
	if err != nil || r.UID != uid || r.Name != u.Username {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveRunAsNumericString(t *testing.T) {
	_, uid, _ := currentUser(t)
	r, err := NewUidMap(nil, false).Resolve("anything", ResolveOpts{RunAs: strconv.Itoa(uid)})
	if err != nil || r.UID != uid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveRunAsUIDColonGID(t *testing.T) {
	_, uid, gid := currentUser(t)
	spec := strconv.Itoa(uid) + ":" + strconv.Itoa(gid)
	r, err := NewUidMap(nil, false).Resolve("anything", ResolveOpts{RunAs: spec})
	if err != nil || r.UID != uid || r.GID != gid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveRunAsUnknownName(t *testing.T) {
	_, err := NewUidMap(nil, false).Resolve("anything", ResolveOpts{RunAs: "__no_such_user_xyzzy__"})
	assertErr(t, err, "no such OS user")
}

func TestResolveTableByName(t *testing.T) {
	u, uid, _ := currentUser(t)
	r, err := NewUidMap(map[string]string{"appuser": u.Username}, false).Resolve("appuser", ResolveOpts{})
	if err != nil || r.UID != uid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveTableNumericUID(t *testing.T) {
	_, uid, _ := currentUser(t)
	r, err := NewUidMap(map[string]string{"appuser": strconv.Itoa(uid)}, false).Resolve("appuser", ResolveOpts{})
	if err != nil || r.UID != uid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveTableUIDColonGID(t *testing.T) {
	_, uid, gid := currentUser(t)
	spec := strconv.Itoa(uid) + ":" + strconv.Itoa(gid)
	r, err := NewUidMap(map[string]string{"appuser": spec}, false).Resolve("appuser", ResolveOpts{})
	if err != nil || r.UID != uid || r.GID != gid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveTableWildcard(t *testing.T) {
	u, uid, _ := currentUser(t)
	r, err := NewUidMap(map[string]string{"*": u.Username}, false).Resolve("anyone", ResolveOpts{})
	if err != nil || r.UID != uid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveRunAsTakesPriorityOverTable(t *testing.T) {
	_, uid, _ := currentUser(t)
	// table maps to root, which would fail — but run_as_uid overrides it.
	r, err := NewUidMap(map[string]string{"appuser": "root"}, false).
		Resolve("appuser", ResolveOpts{RunAsUID: intPtr(uid)})
	if err != nil || r.UID != uid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestResolveColonGIDNonNumeric(t *testing.T) {
	_, err := NewUidMap(nil, false).Resolve("anything", ResolveOpts{RunAs: "notanint:notanint"})
	if err == nil {
		t.Fatalf("expected error for non-numeric spec")
	}
}

func TestResolveValidatesUsername(t *testing.T) {
	_, err := NewUidMap(nil, false).Resolve("ali\x00ce", ResolveOpts{})
	assertErr(t, err, "null byte")
}

func TestResolveEmptyUsernameWithRunAsUID(t *testing.T) {
	_, uid, _ := currentUser(t)
	r, err := NewUidMap(nil, false).Resolve("", ResolveOpts{RunAsUID: intPtr(uid)})
	if err != nil || r.UID != uid {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func TestFromUIDUnknownSynthetic(t *testing.T) {
	r, err := NewUidMap(nil, false).Resolve("", ResolveOpts{RunAsUID: intPtr(999999999)})
	if err != nil {
		t.Fatalf("err=%v", err)
	}
	if r.UID != 999999999 || r.Home != "/" || r.Shell != "/bin/sh" || r.Name != "999999999" {
		t.Fatalf("synthetic mismatch: %+v", r)
	}
}

func TestResolveRejectsRootByDefault(t *testing.T) {
	m := NewUidMap(nil, false)
	assertErr(t, mustResolveErr(m, "anything", ResolveOpts{RunAs: "0:0"}), "privileged")
	assertErr(t, mustResolveErr(m, "anything", ResolveOpts{RunAsUID: intPtr(0)}), "privileged")
}

func TestResolvePermitsRootWhenAllowed(t *testing.T) {
	r, err := NewUidMap(nil, true).Resolve("anything", ResolveOpts{RunAs: "0:0"})
	if err != nil || r.UID != 0 || r.GID != 0 {
		t.Fatalf("got %+v err=%v", r, err)
	}
}

func mustResolveErr(m *UidMap, user string, opts ResolveOpts) error {
	_, err := m.Resolve(user, opts)
	return err
}
