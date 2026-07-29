//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Defaults;

namespace Provide.Uterm.ServerConfig;

/// <summary>Auth section of server.toml.</summary>
public sealed class AuthConfig
{
    public string Mode { get; set; } = "jwt";
    public string PrincipalHeader { get; set; } = "x-uterm-principal";
    public string RoleHeader { get; set; } = "x-uterm-role";
    public string TenantHeader { get; set; } = "x-uterm-tenant";
    public string PrincipalCookie { get; set; } = "uterm_principal";
    public string TenantCookie { get; set; } = "uterm_tenant";
    public string RoleCookie { get; set; } = "uterm_role";
    public string SurfaceCookie { get; set; } = "uterm_surface";
    public string TokenCookie { get; set; } = "uterm_token";
    public string JwtIssuer { get; set; } = "provide-uterm";
    public string JwtAudience { get; set; } = "provide-uterm-server";
    public string? JwtJwksUrl { get; set; }
    public string? JwtPublicKeyPem { get; set; }
    public List<string> JwtAlgorithms { get; set; } = new() { "HS256" };
    public int ClockSkewSeconds { get; set; } = 15;
    public string JwtRolesClaim { get; set; } = "roles";
    public string JwtScopesClaim { get; set; } = "scope";
    public string JWTTenantClaim { get; set; } = "tenant_id";
    /// <summary>
    /// Applied when a verified JWT carries no known roles (typical Cloudflare Access
    /// assertions). Filtered to viewer/operator/admin; unknown values fall back to viewer.
    /// </summary>
    public string? JwtDefaultRole { get; set; }
    /// <summary>
    /// When set, auto-fills empty <see cref="JwtJwksUrl"/> / <see cref="JwtIssuer"/> for
    /// Cloudflare Access team domain. JWT material still comes only from verified token
    /// sources (Bearer / CF-Access-JWT-Assertion / CF_Authorization) — never the spoofable
    /// Cf-Access-Authenticated-User-Email header.
    /// </summary>
    public string? CfAccessTeamDomain { get; set; }
    public string? WorkerBearerToken { get; set; }
    public bool ApiKeysEnabled { get; set; }
    public bool HeaderModeAcknowledged { get; set; }
    public List<string> TrustedProxyIps { get; set; } = new();
    public string IdentityProvider { get; set; } = "local";
    public bool DelegateRoles { get; set; } = true;
    public string? WebhookIdpUrl { get; set; }
    public string? WebhookIdpSecret { get; set; }
    public double WebhookIdpTimeoutS { get; set; } = 2.0;
    public string WebhookIdpOnFailure { get; set; } = "deny";
}

public sealed class ServerBindConfig
{
    public string Host { get; set; } = TerminalDefaults.ServerHost;
    public int Port { get; set; } = TerminalDefaults.ServerPort;
    public string PublicBaseUrl { get; set; } = "";
    public string Title { get; set; } = "provide-uterm-server";
    public string NodeId { get; set; } = "default";
    public List<string> AllowedOrigins { get; set; } = new();
    public int? MaxSessions { get; set; }

    public void DerivePublicBaseUrl()
    {
        if (string.IsNullOrWhiteSpace(PublicBaseUrl))
        {
            PublicBaseUrl = $"http://{Host}:{Port}";
        }
    }
}

public sealed class UiConfig
{
    public string AppPath { get; set; } = "/app";
    public string AssetsPath { get; set; } = "/_terminal";
    /// <summary>Optional jsDelivr (or other) base for xterm CSS/JS when no frontend dir is baked.</summary>
    public string? XtermCdn { get; set; }
    /// <summary>SRI hash for xterm.css (sha384-…); empty disables integrity.</summary>
    public string? XtermCdnIntegrity { get; set; }
    public string? FitAddonCdn { get; set; }
    public string? FitAddonCdnIntegrity { get; set; }
}

public sealed class RecordingConfig
{
    public bool EnabledByDefault { get; set; }
    public string Directory { get; set; } = ".uterm-recordings";
    public string ControlChannelMode { get; set; } = "exclude";
    public bool RedactSensitive { get; set; } = true;
    public string StoreType { get; set; } = "local";
}

public sealed class ControlPlaneConfig
{
    public string Backend { get; set; } = "memory";
    public string? DatabaseUrl { get; set; }
    public int ReapIntervalS { get; set; } = 3600;
    public int ReapRetentionS { get; set; } = 604800;
}

public sealed class SecurityConfig
{
    public string Mode { get; set; } = "strict";
    public bool DevModeAcknowledged { get; set; }
    public bool MetricsRequireAuth { get; set; }
    public bool BlockPrivateConnectorTargets { get; set; }
    public string DefaultSessionVisibility { get; set; } = "public";
}

public sealed class TunnelConfig
{
    public int TokenTtlS { get; set; } = 3600;
    public string TokenTransport { get; set; } = "cookie";
    public bool CookieSecure { get; set; } = true;
    public string CookieSamesite { get; set; } = "lax";
    public bool IpBinding { get; set; }
}

/// <summary>Governance section — external policy/authz webhooks (Go GovernanceConfig subset).</summary>
public sealed class GovernanceConfig
{
    public string? AuthzWebhookUrl { get; set; }
    public string? AuthzWebhookSecret { get; set; }
    public double AuthzWebhookTimeoutS { get; set; } = 2.0;
}

/// <summary>Session definition from config or control plane.</summary>
public sealed class SessionDefinition
{
    public string SessionId { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string ConnectorType { get; set; } = "shell";
    public string Visibility { get; set; } = "public";
    public string? Owner { get; set; }
    public List<string> Tags { get; set; } = new();
    public Dictionary<string, object?> Config { get; set; } = new();

    /// <summary>Whether a viewer may type. Reference default is <c>open</c>
    /// (config_schema_session.SessionDefinition.input_mode / Go InputMode).</summary>
    public string InputMode { get; set; } = "open";

    /// <summary>Whether the session is brought up at boot. Reference default is true.</summary>
    public bool AutoStart { get; set; } = true;

    /// <summary>Per-session recording override; null defers to
    /// <see cref="RecordingConfig.EnabledByDefault"/>, as the reference does.</summary>
    public bool? RecordingEnabled { get; set; }
}

public sealed class GraphicalTargetDefinition
{
    public string TargetId { get; set; } = "";
    public string TenantId { get; set; } = "";
    public string Protocol { get; set; } = "rfb";
    public string TargetAddress { get; set; } = "";
    public string? VmName { get; set; }
    public string Name { get; set; } = "";
    public string? Description { get; set; }
    public bool Enabled { get; set; } = true;
    public int Width { get; set; } = 640;
    public int Height { get; set; } = 480;
    public bool IsStatic { get; set; }

    // Generic protocol-specific parameters (TOML [graphical_targets.config]).
    public Dictionary<string, object?> Config { get; set; } = new();
}

/// <summary>Top-level server configuration model matching server.toml shape.</summary>
public sealed class UtermServerConfig
{
    public string Environment { get; set; } = "production";
    public ServerBindConfig Server { get; set; } = new();
    public AuthConfig Auth { get; set; } = new();
    public ControlPlaneConfig ControlPlane { get; set; } = new();
    public UiConfig Ui { get; set; } = new();
    public RecordingConfig Recording { get; set; } = new();
    public SecurityConfig Security { get; set; } = new();
    public TunnelConfig Tunnel { get; set; } = new();
    public GovernanceConfig Governance { get; set; } = new();
    public List<SessionDefinition> Sessions { get; set; } = new();
    public List<GraphicalTargetDefinition> GraphicalTargets { get; set; } = new();
    public int SessionIdleTimeoutS { get; set; }
    public int SessionRetentionS { get; set; }
    public double BrowserRateLimitPerSec { get; set; } = 300;
    public string WorkerFrameOnInvalid { get; set; } = "drop";
    public int MaxConnectionsPerPrincipal { get; set; } = 25;
    public int MaxWorkers { get; set; } = 10000;

    public static UtermServerConfig Default()
    {
        var cfg = new UtermServerConfig
        {
            Environment = "production",
            Auth = new AuthConfig { Mode = "dev_token" },
            Sessions = [DefaultShellSession()],
        };
        cfg.Server.DerivePublicBaseUrl();
        return cfg;
    }

    /// <summary>
    /// The one session the default configuration ships — the first thing anyone
    /// who starts a server sees, so it is the same in every port: Python's
    /// <c>config_schema.UtermServerConfig.sessions</c> default and Go's
    /// <c>serverconfig.newDefaultShellSession</c>. Tag order is part of it.
    /// </summary>
    public static SessionDefinition DefaultShellSession() => new()
    {
        SessionId = "provide-shell",
        DisplayName = "Provide Shell",
        ConnectorType = "shell",
        InputMode = "open",
        AutoStart = true,
        Tags = ["shell", "reference"],
        Visibility = "public",
        Owner = null,
    };
}
