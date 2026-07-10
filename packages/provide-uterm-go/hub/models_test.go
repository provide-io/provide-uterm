//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import "testing"

func TestNewWorkerTermStateDefaults(t *testing.T) {
	st := NewWorkerTermState()
	mustEqual(t, st.InputMode, InputModeHijack, "default input mode")
	mustTrue(t, st.Browsers != nil, "browsers initialised")
}

func TestHijackLeaseViewAndApply(t *testing.T) {
	st := NewWorkerTermState()
	owner := newBrowser("o")
	st.HijackOwner = owner
	st.HijackOwnerExpiresAt = f64p(100)
	st.HijackSession = restSession("h", "o", 200)

	l := st.Lease()
	mustTrue(t, l.WS == owner, "ws")
	mustEqual(t, *l.WSExpiresAt, 100.0, "ws expiry")
	mustTrue(t, l.Session != nil, "session")

	// Mutating the view does not propagate until ApplyLease.
	l.WS = nil
	l.WSExpiresAt = nil
	l.Session = nil
	mustTrue(t, st.HijackOwner == owner, "state unchanged before apply")
	st.ApplyLease(l)
	mustTrue(t, st.HijackOwner == nil && st.HijackOwnerExpiresAt == nil && st.HijackSession == nil, "applied")
}

func TestHijackLeaseIsIdle(t *testing.T) {
	mustTrue(t, HijackLease{}.IsIdle(), "empty idle")
	mustFalse(t, HijackLease{WS: newBrowser("o")}.IsIdle(), "ws not idle")
	mustFalse(t, HijackLease{Session: restSession("h", "o", 1)}.IsIdle(), "session not idle")
}

func TestHijackLeaseActivePredicates(t *testing.T) {
	now := 100.0
	mustFalse(t, HijackLease{}.IsDashboardActive(now), "no ws")
	mustFalse(t, HijackLease{WS: newBrowser("o")}.IsDashboardActive(now), "ws no expiry")
	mustTrue(t, HijackLease{WS: newBrowser("o"), WSExpiresAt: f64p(101)}.IsDashboardActive(now), "future")
	mustFalse(t, HijackLease{WS: newBrowser("o"), WSExpiresAt: f64p(99)}.IsDashboardActive(now), "past")

	mustFalse(t, HijackLease{}.IsRESTActive(now), "no session")
	mustTrue(t, HijackLease{Session: restSession("h", "o", 101)}.IsRESTActive(now), "future rest")
	mustFalse(t, HijackLease{Session: restSession("h", "o", 99)}.IsRESTActive(now), "past rest")

	mustTrue(t, HijackLease{WS: newBrowser("o"), WSExpiresAt: f64p(101)}.IsActive(now), "active via dashboard")
	mustTrue(t, HijackLease{Session: restSession("h", "o", 101)}.IsActive(now), "active via rest")
	mustFalse(t, HijackLease{}.IsActive(now), "idle not active")
}

func TestHijackLeaseExpire(t *testing.T) {
	now := 100.0

	idle := HijackLease{}
	r, d := idle.Expire(now)
	mustFalse(t, r || d, "idle nothing expires")

	restStale := HijackLease{Session: restSession("h", "o", 99)}
	r, d = restStale.Expire(now)
	mustTrue(t, r && !d, "rest expired")
	mustTrue(t, restStale.Session == nil, "session cleared")

	dashStale := HijackLease{WS: newBrowser("o"), WSExpiresAt: f64p(99)}
	r, d = dashStale.Expire(now)
	mustTrue(t, !r && d, "dash expired")
	mustTrue(t, dashStale.WS == nil && dashStale.WSExpiresAt == nil, "dash cleared")

	live := HijackLease{Session: restSession("h", "o", 101)}
	r, d = live.Expire(now)
	mustFalse(t, r || d, "live not expired")
	mustTrue(t, live.Session != nil, "session kept")
}

func TestStrpHelper(t *testing.T) {
	mustEqual(t, *strp("x"), "x", "strp")
	mustEqual(t, *f64p(1.5), 1.5, "f64p")
}
