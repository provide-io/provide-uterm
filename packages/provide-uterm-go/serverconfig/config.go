//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

// Package serverconfig is a Go port of the provide-uterm server configuration
// layer (config.py, config_schema.py, config_schema_session.py, profiles.py).
//
// The struct field names, TOML keys, defaults, validation errors, merge, and
// resolution logic mirror the Python Pydantic models exactly so a server.toml
// written for the Python server parses identically here. A server.toml is
// loaded via LoadServerConfig; a decoded mapping (tomllib-style) is validated
// via ConfigFromMapping — both reproduce the Python precedence: user values
// deep-merge over DefaultServerConfig(), then per-model validators run.
package serverconfig

// CDN URLs for xterm.js and fonts loaded into the operator dashboard HTML.
// These mirror config_schema.py's module constants byte-for-byte.
const (
	XtermCDNDefault    = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0"
	FitAddonCDNDefault = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0"
	FontsCDNDefault    = "https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap"

	// TerminalDefaults.SERVER_HOST / SERVER_PORT (provide/uterm/defaults.py).
	ServerHostDefault = "127.0.0.1"
	ServerPortDefault = 8780
)

// ServerBuiltinConnectorTypes mirrors SERVER_BUILTIN_CONNECTOR_TYPES.
var ServerBuiltinConnectorTypes = map[string]struct{}{
	"shell": {}, "ssh": {}, "telnet": {}, "websocket": {}, "ushell": {},
}

// AuthConfig ports config_schema.AuthConfig. Pointer string fields model the
// Python “str | None“ (a nil pointer is Python None; TOML absence keeps nil).
type AuthConfig struct {
	Mode                   string   `json:"mode" toml:"mode"`
	PrincipalHeader        string   `json:"principal_header" toml:"principal_header"`
	RoleHeader             string   `json:"role_header" toml:"role_header"`
	PrincipalCookie        string   `json:"principal_cookie" toml:"principal_cookie"`
	RoleCookie             string   `json:"role_cookie" toml:"role_cookie"`
	SurfaceCookie          string   `json:"surface_cookie" toml:"surface_cookie"`
	TokenCookie            string   `json:"token_cookie" toml:"token_cookie"`
	JWTIssuer              string   `json:"jwt_issuer" toml:"jwt_issuer"`
	JWTAudience            string   `json:"jwt_audience" toml:"jwt_audience"`
	JWTJWKSURL             *string  `json:"jwt_jwks_url" toml:"jwt_jwks_url"`
	JWTPublicKeyPEM        *string  `json:"jwt_public_key_pem" toml:"jwt_public_key_pem"`
	JWTAlgorithms          []string `json:"jwt_algorithms" toml:"jwt_algorithms"`
	ClockSkewSeconds       int      `json:"clock_skew_seconds" toml:"clock_skew_seconds"`
	JWTRolesClaim          string   `json:"jwt_roles_claim" toml:"jwt_roles_claim"`
	JWTScopesClaim         string   `json:"jwt_scopes_claim" toml:"jwt_scopes_claim"`
	JWTTenantClaim         string   `json:"jwt_tenant_claim" toml:"jwt_tenant_claim"`
	TenantHeader           string   `json:"tenant_header" toml:"tenant_header"`
	TenantCookie           string   `json:"tenant_cookie" toml:"tenant_cookie"`
	WorkerBearerToken      *string  `json:"worker_bearer_token" toml:"worker_bearer_token"`
	APIKeysEnabled         bool     `json:"api_keys_enabled" toml:"api_keys_enabled"`
	HeaderModeAcknowledged bool     `json:"header_mode_acknowledged" toml:"header_mode_acknowledged"`
	RequireJWTInProduction bool     `json:"require_jwt_in_production" toml:"require_jwt_in_production"`
	TrustedProxyIPs        []string `json:"trusted_proxy_ips" toml:"trusted_proxy_ips"`

	UpstreamProxySecret        *string `json:"upstream_proxy_secret" toml:"upstream_proxy_secret"`
	RequireUpstreamProxySecret bool    `json:"require_upstream_proxy_secret" toml:"require_upstream_proxy_secret"`

	IdentityProvider                string   `json:"identity_provider" toml:"identity_provider"`
	DelegateRoles                   bool     `json:"delegate_roles" toml:"delegate_roles"`
	WebhookIDPURL                   *string  `json:"webhook_idp_url" toml:"webhook_idp_url"`
	WebhookIDPSecret                *string  `json:"webhook_idp_secret" toml:"webhook_idp_secret"`
	WebhookIDPTimeoutS              float64  `json:"webhook_idp_timeout_s" toml:"webhook_idp_timeout_s"`
	WebhookIDPOnFailure             string   `json:"webhook_idp_on_failure" toml:"webhook_idp_on_failure"`
	WebhookIDPRequireSignedResponse bool     `json:"webhook_idp_require_signed_response" toml:"webhook_idp_require_signed_response"`
	WebhookIDPRequireResponseNonce  bool     `json:"webhook_idp_require_response_nonce" toml:"webhook_idp_require_response_nonce"`
	WebhookIDPForwardHeaders        []string `json:"webhook_idp_forward_headers" toml:"webhook_idp_forward_headers"`
	WebhookIDPForwardCookies        []string `json:"webhook_idp_forward_cookies" toml:"webhook_idp_forward_cookies"`

	AllowAdhocBrowserObservers bool `json:"allow_adhoc_browser_observers" toml:"allow_adhoc_browser_observers"`
}

func defaultAuthConfig() AuthConfig {
	return AuthConfig{
		Mode:                            "jwt",
		PrincipalHeader:                 "x-uterm-principal",
		RoleHeader:                      "x-uterm-role",
		PrincipalCookie:                 "uterm_principal",
		RoleCookie:                      "uterm_role",
		SurfaceCookie:                   "uterm_surface",
		TokenCookie:                     "uterm_token",
		JWTIssuer:                       "provide-uterm",
		JWTAudience:                     "provide-uterm-server",
		JWTAlgorithms:                   []string{"HS256"},
		ClockSkewSeconds:                15,
		JWTRolesClaim:                   "roles",
		JWTScopesClaim:                  "scope",
		JWTTenantClaim:                  "tenant_id",
		TenantHeader:                    "x-uterm-tenant",
		TenantCookie:                    "uterm_tenant",
		TrustedProxyIPs:                 []string{},
		IdentityProvider:                "local",
		DelegateRoles:                   true,
		WebhookIDPTimeoutS:              2.0,
		WebhookIDPOnFailure:             "deny",
		WebhookIDPRequireSignedResponse: true,
		WebhookIDPForwardHeaders:        []string{},
		WebhookIDPForwardCookies:        []string{},
	}
}

// AuditConfig ports config_schema.AuditConfig.
type AuditConfig struct {
	ChainEnabled bool    `json:"chain_enabled" toml:"chain_enabled"`
	ChainFile    *string `json:"chain_file" toml:"chain_file"`
}

// UiConfig ports config_schema.UiConfig.
type UiConfig struct {
	AppPath              string `json:"app_path" toml:"app_path"`
	AssetsPath           string `json:"assets_path" toml:"assets_path"`
	XtermCDN             string `json:"xterm_cdn" toml:"xterm_cdn"`
	FitAddonCDN          string `json:"fitaddon_cdn" toml:"fitaddon_cdn"`
	FontsCDN             string `json:"fonts_cdn" toml:"fonts_cdn"`
	XtermCDNIntegrity    string `json:"xterm_cdn_integrity" toml:"xterm_cdn_integrity"`
	FitAddonCDNIntegrity string `json:"fitaddon_cdn_integrity" toml:"fitaddon_cdn_integrity"`
}

func defaultUiConfig() UiConfig {
	return UiConfig{
		AppPath:     "/app",
		AssetsPath:  "/_terminal",
		XtermCDN:    XtermCDNDefault,
		FitAddonCDN: FitAddonCDNDefault,
		FontsCDN:    FontsCDNDefault,
	}
}

// RecordingConfig ports config_schema.RecordingConfig. Directory is a string
// (Python Path); LoadServerConfig resolves it relative to the config file.
type RecordingConfig struct {
	EnabledByDefault   bool    `json:"enabled_by_default" toml:"enabled_by_default"`
	Directory          string  `json:"directory" toml:"directory"`
	MaxBytes           int64   `json:"max_bytes" toml:"max_bytes"`
	RetentionS         int64   `json:"retention_s" toml:"retention_s"`
	ControlChannelMode string  `json:"control_channel_mode" toml:"control_channel_mode"`
	RedactSensitive    bool    `json:"redact_sensitive" toml:"redact_sensitive"`
	StoreType          string  `json:"store_type" toml:"store_type"`
	WebhookURL         *string `json:"webhook_url" toml:"webhook_url"`
	WebhookSecret      *string `json:"webhook_secret" toml:"webhook_secret"`
	WebhookTimeoutS    float64 `json:"webhook_timeout_s" toml:"webhook_timeout_s"`
	FlushIntervalS     float64 `json:"flush_interval_s" toml:"flush_interval_s"`
	FlushBatchSize     int     `json:"flush_batch_size" toml:"flush_batch_size"`
}

func defaultRecordingConfig() RecordingConfig {
	return RecordingConfig{
		Directory:          ".uterm-recordings",
		ControlChannelMode: "exclude",
		RedactSensitive:    true,
		StoreType:          "local",
		WebhookTimeoutS:    2.0,
		FlushIntervalS:     5.0,
		FlushBatchSize:     100,
	}
}

// ControlPlaneConfig ports config_schema.ControlPlaneConfig.
type ControlPlaneConfig struct {
	Backend        string  `json:"backend" toml:"backend"`
	DatabaseURL    *string `json:"database_url" toml:"database_url"`
	ReapIntervalS  int     `json:"reap_interval_s" toml:"reap_interval_s"`
	ReapRetentionS int     `json:"reap_retention_s" toml:"reap_retention_s"`
}

func defaultControlPlaneConfig() ControlPlaneConfig {
	return ControlPlaneConfig{Backend: "memory", ReapIntervalS: 3600, ReapRetentionS: 604800}
}

// SecurityConfig ports config_schema.SecurityConfig.
type SecurityConfig struct {
	Mode                         string  `json:"mode" toml:"mode"`
	DevModeAcknowledged          bool    `json:"dev_mode_acknowledged" toml:"dev_mode_acknowledged"`
	CSP                          *string `json:"csp" toml:"csp"`
	HSTS                         *string `json:"hsts" toml:"hsts"`
	XFrameOptions                *string `json:"x_frame_options" toml:"x_frame_options"`
	XContentTypeOptions          *string `json:"x_content_type_options" toml:"x_content_type_options"`
	ReferrerPolicy               *string `json:"referrer_policy" toml:"referrer_policy"`
	PermissionsPolicy            *string `json:"permissions_policy" toml:"permissions_policy"`
	BlockPrivateConnectorTargets bool    `json:"block_private_connector_targets" toml:"block_private_connector_targets"`
	MetricsRequireAuth           bool    `json:"metrics_require_auth" toml:"metrics_require_auth"`
	DefaultSessionVisibility     string  `json:"default_session_visibility" toml:"default_session_visibility"`
}

func defaultSecurityConfig() SecurityConfig {
	return SecurityConfig{Mode: "strict", DefaultSessionVisibility: "public"}
}

// TunnelConfig ports config_schema.TunnelConfig.
type TunnelConfig struct {
	TokenTTLS      int    `json:"token_ttl_s" toml:"token_ttl_s"`
	TokenTransport string `json:"token_transport" toml:"token_transport"`
	CookieSecure   bool   `json:"cookie_secure" toml:"cookie_secure"`
	CookieSamesite string `json:"cookie_samesite" toml:"cookie_samesite"`
	IPBinding      bool   `json:"ip_binding" toml:"ip_binding"`
}

func defaultTunnelConfig() TunnelConfig {
	return TunnelConfig{TokenTTLS: 3600, TokenTransport: "cookie", CookieSecure: true, CookieSamesite: "lax"}
}

// WebhooksConfig ports config_schema.WebhooksConfig.
type WebhooksConfig struct {
	AllowLoopbackDestinations bool `json:"allow_loopback_destinations" toml:"allow_loopback_destinations"`
}

// ProfileStoreConfig ports config_schema.ProfileStoreConfig.
type ProfileStoreConfig struct {
	Directory string `json:"directory" toml:"directory"`
}

func defaultProfileStoreConfig() ProfileStoreConfig {
	return ProfileStoreConfig{Directory: ".uterm-profiles"}
}

// ServerBindConfig ports config_schema.ServerBindConfig.
type ServerBindConfig struct {
	Host           string   `json:"host" toml:"host"`
	Port           int      `json:"port" toml:"port"`
	PublicBaseURL  string   `json:"public_base_url" toml:"public_base_url"`
	Title          string   `json:"title" toml:"title"`
	NodeID         string   `json:"node_id" toml:"node_id"`
	AllowedOrigins []string `json:"allowed_origins" toml:"allowed_origins"`
	MaxSessions    *int     `json:"max_sessions" toml:"max_sessions"`
}

func defaultServerBindConfig() ServerBindConfig {
	return ServerBindConfig{
		Host: ServerHostDefault, Port: ServerPortDefault, Title: "provide-uterm-server",
		NodeID: "default", AllowedOrigins: []string{},
	}
}

// PamConfig ports config_schema.PamConfig.
type PamConfig struct {
	NotifySocket       *string `json:"notify_socket" toml:"notify_socket"`
	Mode               string  `json:"mode" toml:"mode"`
	AutoSession        bool    `json:"auto_session" toml:"auto_session"`
	AutoSessionCommand string  `json:"auto_session_command" toml:"auto_session_command"`
	RelayURL           *string `json:"relay_url" toml:"relay_url"`
	RelayToken         *string `json:"relay_token" toml:"relay_token"`
	CaptureSocketDir   *string `json:"capture_socket_dir" toml:"capture_socket_dir"`
	RequirePeerUIDs    *[]int  `json:"require_peer_uids" toml:"require_peer_uids"`
}

func defaultPamConfig() PamConfig {
	return PamConfig{Mode: "notify", AutoSessionCommand: "/bin/bash"}
}

// GovernanceConfig ports config_schema.GovernanceConfig.
type GovernanceConfig struct {
	PolicyWebhookURL         *string  `json:"policy_webhook_url" toml:"policy_webhook_url"`
	PolicyWebhookSecret      *string  `json:"policy_webhook_secret" toml:"policy_webhook_secret"`
	PolicyWebhookTimeoutS    float64  `json:"policy_webhook_timeout_s" toml:"policy_webhook_timeout_s"`
	DiscoveryProvider        string   `json:"discovery_provider" toml:"discovery_provider"`
	RegistryWebhookURL       *string  `json:"registry_webhook_url" toml:"registry_webhook_url"`
	RegistryWebhookSecret    *string  `json:"registry_webhook_secret" toml:"registry_webhook_secret"`
	RegistryWebhookIntervalS float64  `json:"registry_webhook_interval_s" toml:"registry_webhook_interval_s"`
	AuthzWebhookURL          *string  `json:"authz_webhook_url" toml:"authz_webhook_url"`
	AuthzWebhookSecret       *string  `json:"authz_webhook_secret" toml:"authz_webhook_secret"`
	AuthzWebhookTimeoutS     float64  `json:"authz_webhook_timeout_s" toml:"authz_webhook_timeout_s"`
	BehavioralAuditURL       *string  `json:"behavioral_audit_url" toml:"behavioral_audit_url"`
	BehavioralAuditSecret    *string  `json:"behavioral_audit_secret" toml:"behavioral_audit_secret"`
	BehavioralAuditIntervalS float64  `json:"behavioral_audit_interval_s" toml:"behavioral_audit_interval_s"`
	BehavioralMaxCPS         *float64 `json:"behavioral_max_cps" toml:"behavioral_max_cps"`
	BehavioralMinJitter      *float64 `json:"behavioral_min_jitter" toml:"behavioral_min_jitter"`
	BehavioralFailOpen       bool     `json:"behavioral_fail_open" toml:"behavioral_fail_open"`
	TelemetryWebhookURL      *string  `json:"telemetry_webhook_url" toml:"telemetry_webhook_url"`
	TelemetryWebhookSecret   *string  `json:"telemetry_webhook_secret" toml:"telemetry_webhook_secret"`
	TelemetryWebhookTimeoutS float64  `json:"telemetry_webhook_timeout_s" toml:"telemetry_webhook_timeout_s"`
	ExternalConnectors       []string `json:"external_connectors" toml:"external_connectors"`
}

func defaultGovernanceConfig() GovernanceConfig {
	return GovernanceConfig{
		PolicyWebhookTimeoutS:    2.0,
		DiscoveryProvider:        "webhook",
		RegistryWebhookIntervalS: 60.0,
		AuthzWebhookTimeoutS:     2.0,
		BehavioralAuditIntervalS: 30.0,
		TelemetryWebhookTimeoutS: 2.0,
		ExternalConnectors:       []string{},
	}
}

// UtermServerConfig ports config_schema.UtermServerConfig, the top-level model.
type UtermServerConfig struct {
	Environment      string                      `json:"environment" toml:"environment"`
	Server           ServerBindConfig            `json:"server" toml:"server"`
	Auth             AuthConfig                  `json:"auth" toml:"auth"`
	ControlPlane     ControlPlaneConfig          `json:"control_plane" toml:"control_plane"`
	UI               UiConfig                    `json:"ui" toml:"ui"`
	Recording        RecordingConfig             `json:"recording" toml:"recording"`
	Profiles         ProfileStoreConfig          `json:"profiles" toml:"profiles"`
	Security         SecurityConfig              `json:"security" toml:"security"`
	Tunnel           TunnelConfig                `json:"tunnel" toml:"tunnel"`
	Webhooks         WebhooksConfig              `json:"webhooks" toml:"webhooks"`
	Pam              PamConfig                   `json:"pam" toml:"pam"`
	Governance       GovernanceConfig            `json:"governance" toml:"governance"`
	Audit            AuditConfig                 `json:"audit" toml:"audit"`
	Graphical        GraphicalConfig             `json:"graphical" toml:"graphical"`
	GraphicalTargets []GraphicalTargetDefinition `json:"graphical_targets" toml:"graphical_targets"`
	Sessions         []SessionDefinition         `json:"sessions" toml:"sessions"`

	SessionIdleTimeoutS        int     `json:"session_idle_timeout_s" toml:"session_idle_timeout_s"`
	SessionRetentionS          int     `json:"session_retention_s" toml:"session_retention_s"`
	BrowserRateLimitPerSec     float64 `json:"browser_rate_limit_per_sec" toml:"browser_rate_limit_per_sec"`
	WorkerFrameOnInvalid       string  `json:"worker_frame_on_invalid" toml:"worker_frame_on_invalid"`
	MaxConnectionsPerPrincipal int     `json:"max_connections_per_principal" toml:"max_connections_per_principal"`
	MaxWorkers                 int     `json:"max_workers" toml:"max_workers"`
}

// DefaultServerConfig returns a runnable default config, mirroring
// config.default_server_config() / UtermServerConfig() field defaults.
func DefaultServerConfig() *UtermServerConfig {
	return &UtermServerConfig{
		Environment:                "production",
		Server:                     defaultServerBindConfigDerived(),
		Auth:                       defaultAuthConfigForApp(),
		ControlPlane:               defaultControlPlaneConfig(),
		UI:                         defaultUiConfig(),
		Recording:                  defaultRecordingConfig(),
		Profiles:                   defaultProfileStoreConfig(),
		Security:                   defaultSecurityConfig(),
		Tunnel:                     defaultTunnelConfig(),
		Webhooks:                   WebhooksConfig{},
		Pam:                        defaultPamConfig(),
		Governance:                 defaultGovernanceConfig(),
		Audit:                      AuditConfig{},
		Graphical:                  GraphicalConfig{DynamicAllowedCIDRs: []string{}},
		GraphicalTargets:           []GraphicalTargetDefinition{},
		Sessions:                   defaultSessions(),
		BrowserRateLimitPerSec:     300,
		WorkerFrameOnInvalid:       "drop",
		MaxConnectionsPerPrincipal: 25,
		MaxWorkers:                 10000,
	}
}

// defaultAuthConfigForApp mirrors UtermServerConfig.auth's default_factory,
// which constructs AuthConfig(mode="dev_token") rather than the class default.
func defaultAuthConfigForApp() AuthConfig {
	a := defaultAuthConfig()
	a.Mode = "dev_token"
	return a
}

// defaultServerBindConfigDerived applies the _derive_public_base_url validator.
func defaultServerBindConfigDerived() ServerBindConfig {
	s := defaultServerBindConfig()
	deriveServerURL(&s)
	return s
}

func defaultSessions() []SessionDefinition {
	return []SessionDefinition{newDefaultShellSession()}
}
