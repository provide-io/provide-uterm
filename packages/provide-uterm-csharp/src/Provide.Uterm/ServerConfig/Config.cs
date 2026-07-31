//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Globalization;
using Provide.Uterm.Defaults;
using Provide.Uterm.Hub;

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

/// <summary>
/// Webhook egress policy.
/// </summary>
/// <remarks>
/// Defaults to refusing loopback destinations, matching the reference
/// (<c>config_schema.py:319</c>) and <c>WebhookManager.__init__</c>. The factory
/// used to hardcode the opposite, so a session webhook could be pointed at
/// <c>127.0.0.1</c> and reach anything else listening on the host.
/// </remarks>
public sealed class WebhooksConfig
{
    public bool AllowLoopbackDestinations { get; set; }
}

/// <summary>Governance section — external policy/authz webhooks (Go GovernanceConfig subset).</summary>
public sealed class GovernanceConfig
{
    public string? PolicyWebhookUrl { get; set; }
    public string? PolicyWebhookSecret { get; set; }
    public double PolicyWebhookTimeoutS { get; set; } = 2.0;
    public string? AuthzWebhookUrl { get; set; }
    public string? AuthzWebhookSecret { get; set; }
    public double AuthzWebhookTimeoutS { get; set; } = 2.0;
}

/// <summary>
/// Session definition from config or control plane — the port of
/// <c>config_schema_session.SessionDefinition</c> (and Go's
/// <c>serverconfig.SessionDefinition</c>), field for field.
///
/// Every field here is one an operator can write in a <c>[[sessions]]</c>
/// entry. A field this class does not carry is a setting that can be written
/// down and silently not applied, which is worse than one that is refused.
/// </summary>
public sealed class SessionDefinition
{
    public string SessionId { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string ConnectorType { get; set; } = "shell";
    public string Visibility { get; set; } = "public";
    public string? Owner { get; set; }
    public List<string> Tags { get; set; } = new();

    /// <summary>
    /// Connector-specific settings (<c>connector_config</c>). Any key the
    /// reference's model does not define collects in here rather than being
    /// refused — which is what defeats <c>extra="forbid"</c> for this section.
    /// </summary>
    public Dictionary<string, object?> ConnectorConfig { get; set; } = new();

    /// <summary>Whether a viewer may type. Reference default is <c>open</c>
    /// (config_schema_session.SessionDefinition.input_mode / Go InputMode).</summary>
    public string InputMode { get; set; } = "open";

    /// <summary>Whether the session is brought up at boot. Reference default is true.</summary>
    public bool AutoStart { get; set; } = true;

    /// <summary>Per-session recording override; null defers to
    /// <see cref="RecordingConfig.EnabledByDefault"/>, as the reference does.</summary>
    public bool? RecordingEnabled { get; set; }

    /// <summary>Whether the session is discarded once nobody holds it.</summary>
    public bool Ephemeral { get; set; }

    /// <summary>Whether collaborative presence is published for this session.</summary>
    public bool Presence { get; set; }

    /// <summary>Idle seconds before an operator lease may transfer. Reference default is 30.</summary>
    public int AutoTransferIdleS { get; set; } = 30;

    /// <summary>How queued keystrokes are surfaced: <c>display</c> or <c>replay</c>.</summary>
    public string KeystrokeQueue { get; set; } = "display";
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
    public WebhooksConfig Webhooks { get; set; } = new();
    public List<SessionDefinition> Sessions { get; set; } = new();
    public List<GraphicalTargetDefinition> GraphicalTargets { get; set; } = new();
    /// <summary>Permit dormant fan-out members at creation; send-time authz still applies.</summary>
    public bool FanoutAllowUnknownMembers { get; set; }
    public int SessionIdleTimeoutS { get; set; }
    public int SessionRetentionS { get; set; }

    private double _browserRateLimitPerSec = 300;
    private double _restAcquireRateLimitPerSec = 5;
    private double _restSendRateLimitPerSec = 20;

    /// <summary>
    /// Ceiling for inbound browser WebSocket messages (tokens/sec, burst = one
    /// second of the same rate). Default is the reference's 300, so an unset
    /// deployment is unchanged.
    ///
    /// It shares <see cref="ValidateRateLimit"/> with the two REST keys because
    /// it shares a <see cref="TokenBucket"/>: same burst-equals-rate rule, so
    /// the same floor follows. It is in fact the most dangerous of the three in
    /// the reference — <c>RateLimiter.__init__</c> clamps the REST rates, but the
    /// browser rate reaches <c>TokenBucket</c> unclamped, so a configured
    /// <c>0</c> denied every browser message for the life of the process. An
    /// operator could brick their own deployment with a value that reads like
    /// "no limit", and nothing would say so — not at startup, not in a log, only
    /// in a terminal nobody can type into.
    /// </summary>
    public double BrowserRateLimitPerSec
    {
        get => _browserRateLimitPerSec;
        set => _browserRateLimitPerSec = ValidateRateLimit("browser_rate_limit_per_sec", value);
    }

    /// <summary>
    /// Ceiling for <c>POST /worker/{id}/hijack/acquire</c> (tokens/sec, burst =
    /// one second of the same rate), applied twice — once globally and once per
    /// calling client, so one client can never spend more than its own share.
    /// Guards the expensive, state-changing lease grab. Default is the hub's
    /// own built-in value, so an unset deployment is unchanged.
    /// </summary>
    public double RestAcquireRateLimitPerSec
    {
        get => _restAcquireRateLimitPerSec;
        set => _restAcquireRateLimitPerSec = ValidateRateLimit("rest_acquire_rate_limit_per_sec", value);
    }

    /// <summary>
    /// Ceiling shared by the hijack <c>send</c> <em>and</em> <c>step</c>
    /// endpoints — both are cheap keystroke-rate calls, which is why the budget
    /// is larger than acquire's. Same double application and burst rule.
    /// </summary>
    public double RestSendRateLimitPerSec
    {
        get => _restSendRateLimitPerSec;
        set => _restSendRateLimitPerSec = ValidateRateLimit("rest_send_rate_limit_per_sec", value);
    }

    /// <summary>
    /// Refuses any rate that would not behave as the operator wrote it — the
    /// port of <c>config_schema._validate_rate_limit</c>.
    ///
    /// Guards all three configured ceilings — the two REST hijack budgets and
    /// the browser one. They share a validator because they share a
    /// <see cref="TokenBucket"/>, so the same burst rule and therefore the same
    /// floor applies to each.
    ///
    /// A rate limit is trusted once configured, so every value that cannot be
    /// honoured verbatim is refused rather than reinterpreted.
    ///
    /// <em>Not finite.</em> <c>inf</c> passes every <c>&gt;=</c> bound, so
    /// accepting it would silently mean "no limit at all" — the same fail-open
    /// that makes a trusted limit worse than none. <c>-inf</c> and <c>NaN</c>
    /// go with it: none of the three is a rate anybody meant to write.
    ///
    /// <em>Below <see cref="TokenBucket.MinRatePerSec"/>.</em> <c>0</c> is
    /// ambiguous — read as "unlimited" it disables the limit, read as "refuse
    /// everything" it bricks the surface it guards, and nothing in the file says
    /// which the operator meant. The whole band under the floor is refused for
    /// the <em>second</em> of those reasons rather than for ambiguity: a token
    /// bucket's burst is one second of its rate, so a sub-1/s bucket never
    /// holds a whole token and denies every call forever. <c>0.5</c> is not
    /// "one call every two seconds", it is "never" — so it is refused exactly
    /// like <c>0</c>. Negatives go the same way, and the floor also keeps the
    /// limiter's own clamp from handing back a looser rate than was configured.
    ///
    /// Rates at or above the floor are a real policy and are kept.
    ///
    /// The bound is written <c>!(value &gt;= MIN)</c> rather than
    /// <c>value &lt; MIN</c> so a NaN — which compares false against everything
    /// — falls into the refusal instead of sliding past a <c>&lt;</c> test.
    /// The finiteness check above already catches NaN; this is the second line
    /// of defence that survives someone reordering or dropping it. Do not
    /// "simplify" it.
    ///
    /// Refusing lives on the property rather than in the TOML loader so every
    /// path that builds a config — file, CLI, embedding caller — is refused at
    /// startup. A server that boots with a nonsense limit and discovers it at
    /// first use is a server that ran unprotected in between.
    /// </summary>
    private static double ValidateRateLimit(string key, double value)
    {
        var floor = TokenBucket.MinRatePerSec.ToString("0.0##", CultureInfo.InvariantCulture);
        if (!double.IsFinite(value))
        {
            throw new ArgumentException(FormattableString.Invariant(
                $"{key} must be a finite number >= {floor}, got: {value}"));
        }

        if (!(value >= TokenBucket.MinRatePerSec))
        {
            throw new ArgumentException(FormattableString.Invariant(
                $"{key} must be >= {floor}, got: {value}"));
        }

        return value;
    }

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
