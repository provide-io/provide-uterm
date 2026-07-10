//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package pty

import (
	"fmt"
	"strings"
)

// PamError is raised for PAM lifecycle failures. Port of pam.PamError.
type PamError struct{ msg string }

func (e *PamError) Error() string { return e.msg }

func newPamError(format string, a ...any) *PamError {
	return &PamError{msg: fmt.Sprintf(format, a...)}
}

// pamBackend abstracts the platform PAM implementation. The libpam C API is not
// available in the Go standard library; a real implementation would be a
// cgo/build-tagged backend calling pam_start/pam_authenticate/... The default
// backend (see pambackend.go) is a fail-closed stub that refuses to
// authenticate, exactly matching the Python path when libpam is unavailable
// (PamError "libpam not available on this system").
//
// SECURITY: swapping this backend is the ONLY sanctioned way to enable real PAM
// auth. The stub never returns a handle, so authentication cannot succeed and no
// session is ever opened without a genuine, deliberately-installed backend.
type pamBackend interface {
	// Authenticate begins a PAM transaction and runs pam_authenticate. It
	// returns an opaque handle on success, or an error (fail-closed).
	Authenticate(service, username, password string) (pamHandle, error)
}

// pamHandle represents an authenticated PAM transaction (a live pam_handle_t).
type pamHandle interface {
	AcctMgmt() error
	OpenSession() (env map[string]string, err error)
	CloseSession()
}

// PamSession drives the full PAM lifecycle for PTY session creation, mirroring
// sshd: authenticate → acct_mgmt → open_session → env → [spawn] →
// close_session. Port of pam.PamSession.
type PamSession struct {
	service     string
	backend     pamBackend
	username    string
	env         map[string]string
	sessionOpen bool
	handle      pamHandle
}

// NewPamSession builds a PamSession for service using the default (fail-closed
// stub) backend. Port of PamSession.__init__ (validates the service name).
func NewPamSession(service string) (*PamSession, error) {
	return NewPamSessionWithBackend(service, defaultPamBackend())
}

// NewPamSessionWithBackend builds a PamSession with an explicit backend (used by
// tests to exercise the lifecycle without a real libpam).
func NewPamSessionWithBackend(service string, backend pamBackend) (*PamSession, error) {
	if err := ValidateServiceName(service); err != nil {
		return nil, err
	}
	return &PamSession{service: service, backend: backend, env: map[string]string{}}, nil
}

// Authenticate runs pam_start + pam_authenticate. Port of PamSession.authenticate.
func (p *PamSession) Authenticate(username, password string) error {
	if err := ValidateUsername(username); err != nil {
		return err
	}
	if strings.ContainsRune(password, '\x00') {
		return fmt.Errorf("password contains null byte")
	}
	handle, err := p.backend.Authenticate(p.service, username, password)
	if err != nil {
		return err
	}
	p.handle = handle
	p.username = username
	return nil
}

// AcctMgmt checks account validity (expiry, access restrictions). Port of
// PamSession.acct_mgmt.
func (p *PamSession) AcctMgmt() error {
	if p.username == "" {
		return newPamError("authenticate() must be called first")
	}
	if p.handle == nil {
		return nil
	}
	if err := p.handle.AcctMgmt(); err != nil {
		return newPamError("pam_acct_mgmt failed: %s", err)
	}
	return nil
}

// OpenSession runs pam_open_session and collects the PAM environment. Port of
// PamSession.open_session.
func (p *PamSession) OpenSession() error {
	if p.username == "" {
		return newPamError("authenticate() must be called first")
	}
	if p.handle == nil {
		p.sessionOpen = true
		return nil
	}
	env, err := p.handle.OpenSession()
	if err != nil {
		return newPamError("pam_open_session failed: %s", err)
	}
	if env != nil {
		p.env = env
	}
	p.sessionOpen = true
	return nil
}

// Env returns a copy of the collected PAM environment. Port of PamSession.get_env.
func (p *PamSession) Env() map[string]string {
	out := make(map[string]string, len(p.env))
	for k, v := range p.env {
		out[k] = v
	}
	return out
}

// CloseSession runs pam_close_session + pam_end. Idempotent. Port of
// PamSession.close_session.
func (p *PamSession) CloseSession() {
	if p.username == "" || !p.sessionOpen {
		return
	}
	if p.handle != nil {
		p.handle.CloseSession()
	}
	p.sessionOpen = false
	p.handle = nil
}
