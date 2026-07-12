//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"
	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// rolesSplit mirrors Python's re.split(r"[,\s]+", raw) for a string roles claim.
var rolesSplit = regexp.MustCompile(`[,\s]+`)

// LocalIdentityProvider ports auth.LocalIdentityProvider — the standard RBAC
// IdentityProvider covering the api_key, header and jwt auth modes.
type LocalIdentityProvider struct {
	Auth        *serverconfig.AuthConfig
	APIKeyStore *ApiKeyStore
	logger      *slog.Logger
}

// NewLocalIdentityProvider builds a provider over auth and an optional key store.
func NewLocalIdentityProvider(auth *serverconfig.AuthConfig, store *ApiKeyStore) *LocalIdentityProvider {
	return &LocalIdentityProvider{
		Auth:        auth,
		APIKeyStore: store,
		logger:      ptel.GetLogger(context.Background(), "provide.uterm.server.auth"),
	}
}

// Authenticate ports resolve_principal_sync: API-key auth takes precedence,
// then the configured mode (header / jwt) resolves the principal. Unknown modes
// return an error; unauthenticated requests return the anonymous principal.
func (p *LocalIdentityProvider) Authenticate(_ context.Context, req *Request) (*Principal, error) {
	if apiKeyPrincipal := p.PrincipalFromAPIKey(req); apiKeyPrincipal != nil {
		return apiKeyPrincipal, nil
	}

	mode := strings.ToLower(strings.TrimSpace(p.Auth.Mode))
	if mode == "header" {
		trusted := p.Auth.TrustedProxyIPs
		if len(trusted) > 0 && !containsStr(trusted, req.SourceIP) {
			p.logger.Warn("header_auth_rejected_untrusted_source",
				"source", req.SourceIP, "trusted", sortedCopy(trusted))
			return AnonymousPrincipal(), nil
		}
		return p.PrincipalFromHeaderAuth(req), nil
	}
	if mode != "jwt" {
		return nil, fmt.Errorf("unknown auth mode: %q", mode)
	}

	token := ExtractBearerToken(req)
	if token == "" {
		token = req.Cookie(p.Auth.TokenCookie)
	}
	if token == "" {
		return AnonymousPrincipal(), nil
	}
	principal, err := p.PrincipalFromJWTToken(token)
	if err != nil {
		p.logger.Warn("jwt_auth_failed", "error", err)
		return AnonymousPrincipal(), nil
	}
	return principal, nil
}

// ExtractBearerToken ports auth.extract_bearer_token: parse "Bearer <token>"
// from the Authorization header, splitting on a single ASCII space.
func ExtractBearerToken(req *Request) string {
	authorization := strings.TrimSpace(req.Header("authorization"))
	if authorization == "" {
		return ""
	}
	parts := strings.SplitN(authorization, " ", 2)
	if len(parts) != 2 {
		return ""
	}
	if strings.ToLower(parts[0]) != "bearer" {
		return ""
	}
	return strings.TrimSpace(parts[1])
}

// PrincipalFromHeaderAuth ports _principal_from_header_auth.
func (p *LocalIdentityProvider) PrincipalFromHeaderAuth(req *Request) *Principal {
	principal := firstNonEmpty(req.Header(p.Auth.PrincipalHeader), req.Cookie(p.Auth.PrincipalCookie), "anonymous")
	roleRaw := firstNonEmpty(req.Header(p.Auth.RoleHeader), req.Cookie(p.Auth.RoleCookie), "")
	tenant, _ := CanonicalTenantID(firstNonEmpty(req.Header(p.Auth.TenantHeader), req.Cookie(p.Auth.TenantCookie)))
	return &Principal{
		SubjectID: principal,
		TenantID:  tenant,
		Roles:     FilterKnownRoles([]string{roleRaw}),
		Scopes:    NewSet(),
		Claims:    map[string]any{},
	}
}

// PrincipalFromAPIKey ports _principal_from_api_key including the explicit
// scope→role mapping (unknown/empty scopes reject the key outright).
func (p *LocalIdentityProvider) PrincipalFromAPIKey(req *Request) *Principal {
	if !p.Auth.APIKeysEnabled {
		return nil
	}
	rawKey := strings.TrimSpace(req.Header("x-api-key"))
	if rawKey == "" {
		return nil
	}
	if p.APIKeyStore == nil { // pragma: allowlist secret
		return nil
	}
	record := p.APIKeyStore.Validate(rawKey)
	if record == nil {
		p.logger.Warn("api_key_auth_failed", "key_id", "unknown")
		return nil
	}
	var roles, scopes Set
	switch {
	case record.Scopes.Has("admin"):
		roles, scopes = NewSet("admin"), NewSet("*")
	case record.Scopes.Has("operator"):
		roles, scopes = NewSet("operator"), NewSet("*")
	case record.Scopes.Has("viewer"):
		roles, scopes = NewSet("viewer"), NewSet("*")
	default:
		p.logger.Warn("api_key_auth_failed", "key_id", record.KeyID,
			"reason", "unrecognized_or_empty_scope", "scopes", record.Scopes.Sorted())
		return nil
	}
	return &Principal{
		SubjectID: "apikey:" + record.KeyID,
		TenantID:  record.TenantID,
		Roles:     roles,
		Scopes:    scopes,
		Claims:    map[string]any{"key_id": record.KeyID, "key_name": record.Name},
	}
}

// PrincipalFromJWTToken ports _principal_from_jwt_token: validate signature,
// issuer, audience, expiry (with clock skew), require sub+exp, and build the
// principal from the claims.
func (p *LocalIdentityProvider) PrincipalFromJWTToken(token string) (*Principal, error) {
	skew := p.Auth.ClockSkewSeconds
	if skew < 0 {
		skew = 0
	}
	parser := jwt.NewParser(
		jwt.WithValidMethods(p.Auth.JWTAlgorithms),
		jwt.WithIssuer(p.Auth.JWTIssuer),
		jwt.WithAudience(p.Auth.JWTAudience),
		jwt.WithLeeway(time.Duration(skew)*time.Second),
		jwt.WithExpirationRequired(),
	)
	claims := jwt.MapClaims{}
	if _, err := parser.ParseWithClaims(token, claims, p.jwtKeyFunc); err != nil {
		return nil, err
	}
	subject := strings.TrimSpace(asStr(claims["sub"]))
	if subject == "" {
		return nil, errors.New("sub claim is required")
	}
	tenant := ""
	if rawTenant, present := claims[p.Auth.JWTTenantClaim]; present {
		var err error
		tenant, err = CanonicalTenantID(asStr(rawTenant))
		if err != nil {
			return nil, err
		}
	}
	return &Principal{
		SubjectID: subject,
		TenantID:  tenant,
		Roles:     p.rolesFromClaims(claims),
		Scopes:    p.scopesFromClaims(claims),
		Claims:    map[string]any(claims),
	}, nil
}

// jwtKeyFunc ports _resolve_jwt_key: JWKS URL takes precedence, else the
// configured PEM (interpreted per the token's algorithm family).
func (p *LocalIdentityProvider) jwtKeyFunc(token *jwt.Token) (any, error) {
	if p.Auth.JWTJWKSURL != nil && strings.TrimSpace(*p.Auth.JWTJWKSURL) != "" {
		return resolveJWKSKey(*p.Auth.JWTJWKSURL, token)
	}
	if p.Auth.JWTPublicKeyPEM != nil && *p.Auth.JWTPublicKeyPEM != "" {
		pem := []byte(*p.Auth.JWTPublicKeyPEM)
		alg := token.Method.Alg()
		switch {
		case strings.HasPrefix(alg, "HS"):
			return pem, nil
		case strings.HasPrefix(alg, "RS"), strings.HasPrefix(alg, "PS"):
			return jwt.ParseRSAPublicKeyFromPEM(pem)
		case strings.HasPrefix(alg, "ES"):
			return jwt.ParseECPublicKeyFromPEM(pem)
		default:
			return pem, nil
		}
	}
	return nil, errors.New("jwt_public_key_pem or jwt_jwks_url must be configured in jwt mode")
}

func (p *LocalIdentityProvider) rolesFromClaims(claims jwt.MapClaims) Set {
	raw := claims[p.Auth.JWTRolesClaim]
	var pieces []string
	switch v := raw.(type) {
	case string:
		for _, part := range rolesSplit.Split(v, -1) {
			if strings.TrimSpace(part) != "" {
				pieces = append(pieces, strings.ToLower(strings.TrimSpace(part)))
			}
		}
	case []any:
		for _, part := range v {
			s := strings.TrimSpace(asStr(part))
			if s != "" {
				pieces = append(pieces, strings.ToLower(s))
			}
		}
	}
	return FilterKnownRoles(pieces)
}

func (p *LocalIdentityProvider) scopesFromClaims(claims jwt.MapClaims) Set {
	raw := claims[p.Auth.JWTScopesClaim]
	out := NewSet()
	switch v := raw.(type) {
	case string:
		for _, part := range strings.Fields(v) {
			if strings.TrimSpace(part) != "" {
				out[strings.TrimSpace(part)] = struct{}{}
			}
		}
	case []any:
		for _, part := range v {
			s := strings.TrimSpace(asStr(part))
			if s != "" {
				out[s] = struct{}{}
			}
		}
	}
	return out
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

func containsStr(list []string, target string) bool {
	for _, v := range list {
		if v == target {
			return true
		}
	}
	return false
}

func sortedCopy(list []string) []string {
	out := append([]string(nil), list...)
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j-1] > out[j]; j-- {
			out[j-1], out[j] = out[j], out[j-1]
		}
	}
	return out
}

func asStr(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	default:
		return fmt.Sprintf("%v", t)
	}
}
