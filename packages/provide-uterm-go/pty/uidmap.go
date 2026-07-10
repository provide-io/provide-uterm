//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"errors"
	"fmt"
	"os/user"
	"strconv"
	"strings"
)

// ResolvedUser is the OS identity a PTY child is launched as. Port of
// uid_map.ResolvedUser.
type ResolvedUser struct {
	UID   int
	GID   int
	Home  string
	Shell string
	Name  string // OS username (used for supplementary-group resolution)
}

// UidMapError is raised for resolution failures and privilege-policy
// violations. Port of uid_map.UidMapError (a ValueError subclass).
type UidMapError struct{ msg string }

func (e *UidMapError) Error() string { return e.msg }

func newUidMapError(format string, a ...any) *UidMapError {
	return &UidMapError{msg: fmt.Sprintf(format, a...)}
}

// UidMap resolves an application username to an OS (uid, gid, home, shell).
// Port of uid_map.UidMap.
//
// Resolution priority:
//  1. runAsUID (per-session explicit uid)
//  2. runAs (per-session OS username, numeric uid, or "uid:gid")
//  3. table entry keyed on username (same format as runAs; "*" is wildcard)
//  4. os/user lookup on username — user runs as themselves
type UidMap struct {
	table     map[string]string
	allowRoot bool
}

// NewUidMap builds a UidMap. table may be nil.
func NewUidMap(table map[string]string, allowRoot bool) *UidMap {
	if table == nil {
		table = map[string]string{}
	}
	return &UidMap{table: table, allowRoot: allowRoot}
}

// ResolveOpts carries the optional per-session overrides for Resolve. A nil
// *int means "not provided" (mirroring Python's None default).
type ResolveOpts struct {
	RunAs    string // "" means not provided
	RunAsUID *int
	RunAsGID *int
}

// Resolve maps username (+ opts) to a ResolvedUser. Port of UidMap.resolve.
func (m *UidMap) Resolve(username string, opts ResolveOpts) (*ResolvedUser, error) {
	// Validate username early — before touching the name service or the table.
	if username != "" {
		if err := ValidateUsername(username); err != nil {
			return nil, err
		}
	}

	if opts.RunAsUID != nil {
		if err := m.checkPrivilege(*opts.RunAsUID, opts.RunAsGID); err != nil {
			return nil, err
		}
		return m.fromUID(*opts.RunAsUID, opts.RunAsGID)
	}

	if opts.RunAs != "" {
		return m.resolveSpec(opts.RunAs, opts.RunAsGID)
	}

	spec, ok := m.table[username]
	if !ok {
		spec, ok = m.table["*"]
	}
	if ok {
		return m.resolveSpec(spec, opts.RunAsGID)
	}

	u, err := user.Lookup(username)
	if err != nil {
		return nil, newUidMapError("no such OS user: %q", username)
	}
	return m.fromUserRecord(u, opts.RunAsGID)
}

// checkPrivilege enforces the no-root policy unless allowRoot is set. Port of
// UidMap._check_privilege.
func (m *UidMap) checkPrivilege(uid int, gid *int) error {
	if m.allowRoot {
		return nil
	}
	if uid == 0 || (gid != nil && *gid == 0) {
		gidRepr := "None"
		if gid != nil {
			gidRepr = strconv.Itoa(*gid)
		}
		return newUidMapError("resolving to privileged %d:%s is not allowed", uid, gidRepr)
	}
	return nil
}

// fromUID resolves an explicit uid (+ optional gid). Port of UidMap._from_uid.
// An unknown uid yields a synthetic user rooted at "/" with /bin/sh.
func (m *UidMap) fromUID(uid int, gid *int) (*ResolvedUser, error) {
	if err := m.checkPrivilege(uid, gid); err != nil {
		return nil, err
	}
	u, err := user.LookupId(strconv.Itoa(uid))
	if err != nil {
		resolvedGID := uid
		if gid != nil {
			resolvedGID = *gid
		}
		return &ResolvedUser{
			UID:   uid,
			GID:   resolvedGID,
			Home:  "/",
			Shell: defaultShell,
			Name:  strconv.Itoa(uid),
		}, nil
	}
	pwUID, _ := strconv.Atoi(u.Uid)
	pwGID, _ := strconv.Atoi(u.Gid)
	resolvedGID := pwGID
	if gid != nil {
		resolvedGID = *gid
	}
	return &ResolvedUser{
		UID:   pwUID,
		GID:   resolvedGID,
		Home:  u.HomeDir,
		Shell: lookupShell(pwUID, u.Username),
		Name:  u.Username,
	}, nil
}

// resolveSpec parses "OS-username" | "uid" | "uid:gid". Port of
// UidMap._resolve_spec.
func (m *UidMap) resolveSpec(spec string, runAsGID *int) (*ResolvedUser, error) {
	if strings.Contains(spec, ":") {
		parts := strings.SplitN(spec, ":", 2)
		uid, err := strconv.Atoi(parts[0])
		if err != nil {
			return nil, fmt.Errorf("invalid uid in spec %q: %w", spec, err)
		}
		gid, err := strconv.Atoi(parts[1])
		if err != nil {
			return nil, fmt.Errorf("invalid gid in spec %q: %w", spec, err)
		}
		return m.fromUID(uid, &gid)
	}

	if uid, err := strconv.Atoi(spec); err == nil {
		return m.fromUID(uid, runAsGID)
	}

	u, err := user.Lookup(spec)
	if err != nil {
		return nil, newUidMapError("no such OS user: %q", spec)
	}
	return m.fromUserRecord(u, runAsGID)
}

// fromUserRecord builds a ResolvedUser from an os/user record, applying the
// gid override + root-privilege check (shared by name-based resolution paths).
func (m *UidMap) fromUserRecord(u *user.User, runAsGID *int) (*ResolvedUser, error) {
	pwUID, _ := strconv.Atoi(u.Uid)
	pwGID, _ := strconv.Atoi(u.Gid)
	gid := pwGID
	if runAsGID != nil {
		gid = *runAsGID
	}
	if err := m.checkPrivilege(pwUID, &gid); err != nil {
		return nil, err
	}
	return &ResolvedUser{
		UID:   pwUID,
		GID:   gid,
		Home:  u.HomeDir,
		Shell: lookupShell(pwUID, u.Username),
		Name:  u.Username,
	}, nil
}

// IsUidMapError reports whether err is (or wraps) a *UidMapError.
func IsUidMapError(err error) bool {
	var target *UidMapError
	return errors.As(err, &target)
}
