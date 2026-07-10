//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"
	ptel "github.com/provide-io/provide-telemetry/go"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// DevTokenTTLS ports dev_idp.DEV_TOKEN_TTL_S — 24h.
const DevTokenTTLS = 24 * 3600

// defaultDevTokenRelPath is the ~/.cache/uterm/dev_token default location.
var defaultDevTokenRelPath = filepath.Join(".cache", "uterm", "dev_token")

// resolvedTokenPath ports dev_idp._resolved_token_path: caller precedence →
// UTERM_DEV_TOKEN_PATH env → ~/.cache/uterm/dev_token.
func resolvedTokenPath(explicit string) string {
	if explicit != "" {
		return explicit
	}
	if env := os.Getenv("UTERM_DEV_TOKEN_PATH"); env != "" {
		return env
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return defaultDevTokenRelPath
	}
	return filepath.Join(home, defaultDevTokenRelPath)
}

// DevIDPOptions configures SetupDevIDP; zero values take the dev_idp defaults
// (subject "dev-user", roles ["admin"], ttl 24h, resolved token path).
type DevIDPOptions struct {
	TokenPath string
	Subject   string
	Roles     []string
	TTLS      int
}

// SetupDevIDP ports dev_idp.setup_dev_idp: generate a fresh HS256 secret,
// mutate auth so the regular JWT validator accepts it, mint a JWT with the
// admin-role claim, write it 0600 to the resolved path, and return the token.
func SetupDevIDP(auth *serverconfig.AuthConfig, opts DevIDPOptions) (string, error) {
	subject := opts.Subject
	if subject == "" {
		subject = "dev-user"
	}
	roles := opts.Roles
	if roles == nil {
		roles = []string{"admin"}
	}
	ttl := opts.TTLS
	if ttl == 0 {
		ttl = DevTokenTTLS
	}

	secret := tokenURLSafe(48) // ~384 bits, above the 32-char floor
	auth.Mode = "jwt"
	auth.JWTPublicKeyPEM = &secret
	auth.JWTAlgorithms = []string{"HS256"}
	if auth.JWTIssuer == "" {
		auth.JWTIssuer = "provide-uterm-dev"
	}
	if auth.JWTAudience == "" {
		auth.JWTAudience = "provide-uterm-server"
	}
	if auth.WorkerBearerToken == nil || *auth.WorkerBearerToken == "" {
		wt := tokenURLSafe(32)
		auth.WorkerBearerToken = &wt
	}

	now := time.Now().Unix()
	claims := jwt.MapClaims{
		"sub":              subject,
		"iss":              auth.JWTIssuer,
		"aud":              auth.JWTAudience,
		"iat":              now,
		"exp":              now + int64(ttl),
		auth.JWTRolesClaim: roles,
	}
	token, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(secret))
	if err != nil {
		return "", err
	}

	path := resolvedTokenPath(opts.TokenPath)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		return "", err
	}

	logger := ptel.GetLogger(context.Background(), "provide.uterm.server.dev_idp")
	logger.Info("dev_idp_token_issued", "path", path, "subject", subject, "ttl_s", ttl, "roles", roles)
	return token, nil
}

// ReadDevToken ports dev_idp.read_dev_token: return the last-issued dev token
// from disk, or ("", false) if absent/empty.
func ReadDevToken(tokenPath string) (string, bool) {
	path := resolvedTokenPath(tokenPath)
	raw, err := os.ReadFile(path) //nolint:gosec // dev-token path is operator-controlled
	if err != nil {
		return "", false
	}
	token := strings.TrimSpace(string(raw))
	if token == "" {
		return "", false
	}
	return token, true
}
