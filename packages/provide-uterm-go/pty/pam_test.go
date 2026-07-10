//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"errors"
	"testing"
)

// --- fake backend for lifecycle exercising (no real libpam) ---

type fakeHandle struct {
	acctErr error
	openErr error
	env     map[string]string
	closed  bool
}

func (h *fakeHandle) AcctMgmt() error                         { return h.acctErr }
func (h *fakeHandle) OpenSession() (map[string]string, error) { return h.env, h.openErr }
func (h *fakeHandle) CloseSession()                           { h.closed = true }

type fakeBackend struct {
	h   *fakeHandle
	err error
}

func (b fakeBackend) Authenticate(service, username, password string) (pamHandle, error) {
	if b.err != nil {
		return nil, b.err
	}
	return b.h, nil
}

func TestPamStubFailsClosed(t *testing.T) {
	p, err := NewPamSession("provide-uterm")
	if err != nil {
		t.Fatal(err)
	}
	assertErr(t, p.Authenticate("alice", "pw"), "libpam not available")
}

func TestPamServiceValidation(t *testing.T) {
	if _, err := NewPamSession("bad service"); err == nil {
		t.Fatal("expected service-name validation error")
	}
}

func TestPamPasswordNullByte(t *testing.T) {
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: &fakeHandle{}})
	assertErr(t, p.Authenticate("alice", "pw\x00"), "null byte")
}

func TestPamUsernameValidatedInAuth(t *testing.T) {
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: &fakeHandle{}})
	assertErr(t, p.Authenticate("bad user", "pw"), "invalid character")
}

func TestPamLifecycleSuccess(t *testing.T) {
	h := &fakeHandle{env: map[string]string{"LD_PRELOAD": "/x.so"}}
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: h})
	if err := p.Authenticate("alice", "pw"); err != nil {
		t.Fatal(err)
	}
	if err := p.AcctMgmt(); err != nil {
		t.Fatal(err)
	}
	if err := p.OpenSession(); err != nil {
		t.Fatal(err)
	}
	if p.Env()["LD_PRELOAD"] != "/x.so" {
		t.Fatalf("env not collected: %+v", p.Env())
	}
	p.CloseSession()
	if !h.closed {
		t.Fatal("CloseSession should close the handle")
	}
	// Idempotent: second close is a no-op.
	p.CloseSession()
}

func TestPamAcctBeforeAuth(t *testing.T) {
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: &fakeHandle{}})
	assertErr(t, p.AcctMgmt(), "authenticate() must be called first")
}

func TestPamOpenBeforeAuth(t *testing.T) {
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: &fakeHandle{}})
	assertErr(t, p.OpenSession(), "authenticate() must be called first")
}

func TestPamCloseWithoutOpenNoop(t *testing.T) {
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: &fakeHandle{}})
	p.CloseSession() // must not panic
}

func TestPamAuthBackendError(t *testing.T) {
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{err: errors.New("denied")})
	if err := p.Authenticate("alice", "pw"); err == nil {
		t.Fatal("expected backend auth error")
	}
}

func TestPamAcctError(t *testing.T) {
	h := &fakeHandle{acctErr: errors.New("expired")}
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: h})
	_ = p.Authenticate("alice", "pw")
	assertErr(t, p.AcctMgmt(), "pam_acct_mgmt failed")
}

func TestPamOpenError(t *testing.T) {
	h := &fakeHandle{openErr: errors.New("no session")}
	p, _ := NewPamSessionWithBackend("provide-uterm", fakeBackend{h: h})
	_ = p.Authenticate("alice", "pw")
	assertErr(t, p.OpenSession(), "pam_open_session failed")
}
