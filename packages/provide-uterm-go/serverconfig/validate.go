//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"fmt"
	"net/url"
	"strings"
)

// loopbackHosts mirrors _LOOPBACK_HOSTS in config_schema.py.
var loopbackHosts = map[string]struct{}{"localhost": {}, "127.0.0.1": {}, "::1": {}}

// requireSecureURL ports _require_secure_url: reject a cleartext http:// URL
// unless its host is loopback; https:// is always allowed; any other scheme
// raises. Mirrors the SSRF guard byte-for-byte including error messages.
func requireSecureURL(u *string, field string) error {
	if u == nil || *u == "" {
		return nil
	}
	parsed, err := url.Parse(*u)
	if err != nil {
		return fmt.Errorf("%s must use http(s)", field)
	}
	scheme := strings.ToLower(parsed.Scheme)
	if scheme == "https" {
		return nil
	}
	if scheme != "http" {
		return fmt.Errorf("%s must use http(s)", field)
	}
	host := strings.ToLower(parsed.Hostname())
	if _, ok := loopbackHosts[host]; ok {
		return nil
	}
	if strings.HasSuffix(host, ".localhost") {
		return nil
	}
	return fmt.Errorf("%s must use https:// (cleartext http:// is only allowed for loopback hosts)", field)
}

// cleanPath ports config_schema._clean_path.
func cleanPath(value, fallback string) string {
	text := value
	if text == "" {
		text = fallback
	}
	text = strings.TrimSpace(text)
	if !strings.HasPrefix(text, "/") {
		text = "/" + text
	}
	text = strings.TrimRight(text, "/")
	if text == "" {
		return "/"
	}
	return text
}

// deriveServerURL ports ServerBindConfig._derive_public_base_url.
func deriveServerURL(s *ServerBindConfig) {
	if s.PublicBaseURL == "" {
		s.PublicBaseURL = fmt.Sprintf("http://%s:%d", s.Host, s.Port)
	}
}

func normalizeUI(ui *UiConfig) {
	ui.AppPath = cleanPath(ui.AppPath, "/app")
	ui.AssetsPath = cleanPath(ui.AssetsPath, "/_terminal")
}

func literalError(field string, options ...string) error {
	quoted := make([]string, len(options))
	for i, o := range options {
		quoted[i] = "'" + o + "'"
	}
	return fmt.Errorf("%s: input should be %s", field, strings.Join(quoted, " or "))
}

func inSet(v string, options ...string) bool {
	for _, o := range options {
		if v == o {
			return true
		}
	}
	return false
}

// applyCfAccessTeamDomain fills empty JWTJWKSURL / JWTIssuer from a Cloudflare
// Access team domain. Explicit operator values always win.
func applyCfAccessTeamDomain(a *AuthConfig) {
	team := strings.TrimSpace(a.CfAccessTeamDomain)
	if team == "" {
		return
	}
	// Strip accidental scheme/path so "https://myteam.cloudflareaccess.com" also works.
	team = strings.TrimPrefix(team, "https://")
	team = strings.TrimPrefix(team, "http://")
	if i := strings.Index(team, "/"); i >= 0 {
		team = team[:i]
	}
	team = strings.TrimSuffix(team, ".cloudflareaccess.com")
	team = strings.TrimSpace(team)
	if team == "" {
		return
	}
	if strings.TrimSpace(ptrOr(a.JWTJWKSURL)) == "" {
		u := fmt.Sprintf("https://%s.cloudflareaccess.com/cdn-cgi/access/certs", team)
		a.JWTJWKSURL = &u
	}
	if strings.TrimSpace(a.JWTIssuer) == "" {
		a.JWTIssuer = fmt.Sprintf("https://%s.cloudflareaccess.com", team)
	}
}

// validateAuth ports the AuthConfig model_validators.
func validateAuth(a *AuthConfig) error {
	applyCfAccessTeamDomain(a)
	if !inSet(a.IdentityProvider, "local", "webhook") {
		return literalError("auth.identity_provider", "local", "webhook")
	}
	if !inSet(a.WebhookIDPOnFailure, "deny", "viewer") {
		return literalError("auth.webhook_idp_on_failure", "deny", "viewer")
	}
	if a.RequireUpstreamProxySecret && strings.TrimSpace(ptrOr(a.UpstreamProxySecret)) == "" {
		return fmt.Errorf(
			"auth.upstream_proxy_secret is required when auth.require_upstream_proxy_secret=True") // pragma: allowlist secret
	}
	if err := requireSecureURL(a.WebhookIDPURL, "auth.webhook_idp_url"); err != nil {
		return err
	}
	if err := requireSecureURL(a.JWTJWKSURL, "auth.jwt_jwks_url"); err != nil {
		return err
	}
	if a.IdentityProvider == "webhook" && a.WebhookIDPRequireSignedResponse &&
		strings.TrimSpace(ptrOr(a.WebhookIDPSecret)) == "" {
		return fmt.Errorf(
			"requiring a signed IdP response needs auth.webhook_idp_secret; set the secret or " +
				"set auth.webhook_idp_require_signed_response=false to disable verification")
	}
	return nil
}

func validateAudit(a *AuditConfig) error {
	if a.ChainEnabled && strings.TrimSpace(ptrOr(a.ChainFile)) == "" {
		return fmt.Errorf("audit.chain_enabled requires audit.chain_file (the append-only WORM log path)")
	}
	return nil
}

func validateRecording(r *RecordingConfig) error {
	if r.MaxBytes < 0 {
		return fmt.Errorf("recording.max_bytes must be >= 0 (0 = unlimited), got: %d", r.MaxBytes)
	}
	if r.RetentionS < 0 {
		return fmt.Errorf("recording.retention_s must be >= 0 (0 = keep indefinitely), got: %d", r.RetentionS)
	}
	if !inSet(r.ControlChannelMode, "exclude", "wire") {
		return literalError("recording.control_channel_mode", "exclude", "wire")
	}
	if !inSet(r.StoreType, "local", "memory", "null", "webhook") {
		return literalError("recording.store_type", "local", "memory", "null", "webhook")
	}
	return requireSecureURL(r.WebhookURL, "recording.webhook_url")
}

func validateControlPlane(c *ControlPlaneConfig) error {
	if !inSet(c.Backend, "memory", "sqlite") {
		return literalError("control_plane.backend", "memory", "sqlite")
	}
	if c.ReapIntervalS <= 0 {
		return fmt.Errorf("control_plane.reap_interval_s must be > 0, got: %d", c.ReapIntervalS)
	}
	if c.ReapRetentionS < 0 {
		return fmt.Errorf(
			"control_plane.reap_retention_s must be >= 0 (0 = reap as soon as past expiry), got: %d", c.ReapRetentionS)
	}
	if c.Backend == "sqlite" && strings.TrimSpace(ptrOr(c.DatabaseURL)) == "" {
		return fmt.Errorf("control_plane.database_url is required when control_plane.backend='sqlite'")
	}
	return nil
}

func validateSecurity(s *SecurityConfig) error {
	if !inSet(s.Mode, "strict", "dev") {
		return literalError("security.mode", "strict", "dev")
	}
	if !inSet(s.DefaultSessionVisibility, "public", "operator", "private") {
		return literalError("security.default_session_visibility", "public", "operator", "private")
	}
	return nil
}

func validateTunnel(t *TunnelConfig) error {
	if !inSet(t.TokenTransport, "query", "cookie", "both") {
		return literalError("tunnel.token_transport", "query", "cookie", "both")
	}
	if !inSet(t.CookieSamesite, "lax", "strict", "none") {
		return literalError("tunnel.cookie_samesite", "lax", "strict", "none")
	}
	if t.TokenTTLS < 60 {
		return fmt.Errorf("tunnel.token_ttl_s must be >= 60, got: %d", t.TokenTTLS)
	}
	return nil
}

func validatePam(p *PamConfig) error {
	if !inSet(p.Mode, "notify", "capture") {
		return literalError("pam.mode", "notify", "capture")
	}
	return requireSecureURL(p.RelayURL, "pam.relay_url")
}

func validateGovernance(g *GovernanceConfig) error {
	checks := []struct {
		u *string
		f string
	}{
		{g.PolicyWebhookURL, "governance.policy_webhook_url"},
		{g.RegistryWebhookURL, "governance.registry_webhook_url"},
		{g.AuthzWebhookURL, "governance.authz_webhook_url"},
		{g.BehavioralAuditURL, "governance.behavioral_audit_url"},
		{g.TelemetryWebhookURL, "governance.telemetry_webhook_url"},
	}
	for _, c := range checks {
		if err := requireSecureURL(c.u, c.f); err != nil {
			return err
		}
	}
	return nil
}

func ptrOr(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
