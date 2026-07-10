//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package serverauth is a Go port of the provide-uterm server authentication
// and authorization layer (auth.py, auth_roles.py, auth_webhook.py,
// api_keys.py, authorization.py, dev_idp.py, security.py, webhook_signing.py,
// and the auth-facing egress SSRF guard).
//
// Every auth mode is exposed as an Authenticator with a common
// Authenticate(ctx, *Request) (*Principal, error) signature:
//
//   - dev_token: SetupDevIDP mints an HS256 JWT and mutates the AuthConfig to
//     jwt mode; the jwt path then validates it (dev_idp.py + auth.py).
//   - jwt: production JWT validation via golang-jwt/jwt/v5.
//   - header: proxy-stripped-header identity gated by the loopback-bind /
//     trusted_proxy_ips allowlist (fail-closed).
//   - api_key: X-API-Key lookup against an ApiKeyStore.
//   - webhook: delegated-IDP HTTP flow with HMAC request signing, response
//     signature + replay + nonce verification, fail-closed on failure.
//
// Signatures (webhook_signing.py) and token claims (dev_idp.py) are byte-for-
// byte compatible with the Python server so the frontend / Durable Object and
// the Python server interoperate with the Go implementation.
package serverauth

import (
	"context"
	"sort"
	"strings"
)

// Set is an unordered string set, the Go analogue of Python's frozenset used
// for a Principal's roles and scopes.
type Set map[string]struct{}

// NewSet builds a Set from the given items.
func NewSet(items ...string) Set {
	s := make(Set, len(items))
	for _, it := range items {
		s[it] = struct{}{}
	}
	return s
}

// Has reports membership.
func (s Set) Has(item string) bool {
	_, ok := s[item]
	return ok
}

// Sorted returns the members in sorted order (deterministic logging/output).
func (s Set) Sorted() []string {
	out := make([]string, 0, len(s))
	for k := range s {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Principal ports bridge.identity.Principal — a resolved browser or API
// principal.
type Principal struct {
	SubjectID   string
	Roles       Set
	Scopes      Set
	Claims      map[string]any
	DisplayName *string
	// AdminSessionScope confines an admin role to a single session id (tunnel
	// share-operator). Nil means a global admin.
	AdminSessionScope *string
}

// Name returns DisplayName when set, else SubjectID (Principal.name property).
func (p *Principal) Name() string {
	if p.DisplayName != nil && *p.DisplayName != "" {
		return *p.DisplayName
	}
	return p.SubjectID
}

// AnonymousPrincipal ports _anonymous_principal(): subject "anonymous", the
// viewer role, and no scopes.
func AnonymousPrincipal() *Principal {
	return &Principal{
		SubjectID: "anonymous",
		Roles:     NewSet("viewer"),
		Scopes:    NewSet(),
		Claims:    map[string]any{},
	}
}

// Request is the transport-agnostic connection view an Authenticator resolves
// against: HTTP-ish headers, cookies, and the immediate TCP peer IP.
type Request struct {
	Headers  map[string]string
	Cookies  map[string]string
	SourceIP string
}

// Header returns the header value for key, matched case-insensitively (parity
// with Starlette's lower-cased header mapping).
func (r *Request) Header(key string) string {
	if r == nil || r.Headers == nil {
		return ""
	}
	if v, ok := r.Headers[key]; ok {
		return v
	}
	lk := strings.ToLower(key)
	for k, v := range r.Headers {
		if strings.ToLower(k) == lk {
			return v
		}
	}
	return ""
}

// Cookie returns a stripped, non-empty cookie value or "" (mirrors
// _cookie_value semantics: strip then treat empty as absent).
func (r *Request) Cookie(key string) string {
	if r == nil || r.Cookies == nil {
		return ""
	}
	return strings.TrimSpace(r.Cookies[key])
}

// Authenticator is the common interface every auth mode implements.
type Authenticator interface {
	Authenticate(ctx context.Context, req *Request) (*Principal, error)
}
