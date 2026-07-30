//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Tomlyn;
using Tomlyn.Model;

namespace Provide.Uterm.ServerConfig;

/// <summary>TOML loader for <see cref="UtermServerConfig"/>.</summary>
public static class ConfigLoader
{
    /// <summary>
    /// Load server config from a TOML path. Empty/null path returns defaults.
    /// User values deep-merge over <see cref="UtermServerConfig.Default"/>.
    /// </summary>
    public static UtermServerConfig Load(string? path)
    {
        var cfg = UtermServerConfig.Default();
        if (string.IsNullOrWhiteSpace(path))
        {
            return cfg;
        }

        if (!File.Exists(path))
        {
            throw new FileNotFoundException($"server config not found: {path}", path);
        }

        var text = File.ReadAllText(path);
        var model = Toml.ToModel(text);
        ApplyToml(cfg, model);
        cfg.Server.DerivePublicBaseUrl();
        return cfg;
    }

    /// <summary>
    /// Every top-level key the reference's model defines — its
    /// <c>config_schema.UtermServerConfig.model_fields</c>. Anything else is a
    /// mistake, and <see cref="RefuseUnknownTopLevelKeys"/> says so.
    ///
    /// The list is the <em>reference's</em> field set rather than this port's.
    /// The C# model carries no <c>profiles</c>, <c>webhooks</c>, <c>pam</c> or
    /// <c>audit</c> section, but the canonical server accepts all four, and one
    /// server.toml is meant to be readable by any port. A C# server refusing to
    /// start on a file the reference honours would be a worse divergence than
    /// one that quietly does not implement a section, so those keys are
    /// recognised and ignored.
    ///
    /// Ordinal comparison because the reference is a pydantic model: its field
    /// names are case-sensitive, so <c>Environment</c> and <c>ENVIRONMENT</c>
    /// are both as unknown as any outright typo.
    /// </summary>
    private static readonly HashSet<string> KnownTopLevelKeys = new(StringComparer.Ordinal)
    {
        "environment", "server", "auth", "control_plane", "ui", "recording",
        "profiles", "security", "tunnel", "webhooks", "pam", "governance",
        "audit", "sessions", "graphical_targets",
        "session_idle_timeout_s", "session_retention_s",
        "browser_rate_limit_per_sec",
        "rest_acquire_rate_limit_per_sec", "rest_send_rate_limit_per_sec",
        "worker_frame_on_invalid", "max_connections_per_principal", "max_workers",
    };

    /// <summary>
    /// Refuse a top-level key nobody recognises — this port's stand-in for the
    /// reference's <c>ServerBaseModel.model_config = ConfigDict(extra="forbid")</c>.
    ///
    /// Until this existed the loader read the keys it knew and dropped the rest
    /// without a word, so a misspelled key name in a deployment was invisible:
    /// the operator got the default and no warning, and a file the canonical
    /// server refuses to start with booted here looking fine. A typo on a
    /// security-relevant key is the case that matters —
    /// <c>brwoser_rate_limit_per_sec</c> silently means "no rate limit
    /// configured".
    ///
    /// The message carries the key. The reference's own error does too — it is
    /// the <c>ValidationError</c>'s <c>loc</c> — but the formatter that
    /// <c>config.config_from_mapping</c> uses keeps only the message text,
    /// <c>"Extra inputs are not permitted"</c>, which tells an operator their
    /// file is wrong without telling them where. Both halves are kept here: the
    /// reference's sentence, so the two ports read alike, and the key, so the
    /// operator can find the line.
    ///
    /// Checked before anything is applied, because the reference refuses at
    /// model construction: nothing exists at all until the whole mapping
    /// validates, so a rejected file must not leave half its keys installed.
    ///
    /// Scope is the top level. Extras inside a section (<c>[server] hsot = …</c>)
    /// are refused by the reference too, since every section model derives from
    /// the same strict base; that is a wider change — one key set per section,
    /// each of which must also list the reference fields this port does not read
    /// — and is deliberately left for its own pass.
    /// </summary>
    private static void RefuseUnknownTopLevelKeys(TomlTable root)
    {
        foreach (var key in root.Keys)
        {
            if (KnownTopLevelKeys.Contains(key)) continue;
            throw new ArgumentException($"{key}: Extra inputs are not permitted");
        }
    }

    private static void ApplyToml(UtermServerConfig cfg, TomlTable root)
    {
        RefuseUnknownTopLevelKeys(root);

        if (root.TryGetValue("environment", out var env) && env is string es)
        {
            cfg.Environment = es;
        }

        // The three configured rate ceilings. Written as "present ⇒ apply"
        // rather than ToDouble's silent fallback: a rate that failed to parse and
        // quietly reverted to the default would be indistinguishable from one
        // never written, which is the exact silent-loosening these keys are
        // validated against. The range check itself lives on the property
        // setter, so configs built without a file get it too.
        //
        // The browser key used to be read with an `is double` type test, which
        // meant a TOML `browser_rate_limit_per_sec = 300` — decoded as a `long`,
        // and the spelling an operator is most likely to write — failed the test
        // and was discarded without a word. RequireRate accepts an integer, a
        // float and a numeric string alike, and refuses anything else by name.
        if (root.TryGetValue("browser_rate_limit_per_sec", out var br))
        {
            cfg.BrowserRateLimitPerSec = RequireRate("browser_rate_limit_per_sec", br);
        }

        if (root.TryGetValue("rest_acquire_rate_limit_per_sec", out var ra))
        {
            cfg.RestAcquireRateLimitPerSec = RequireRate("rest_acquire_rate_limit_per_sec", ra);
        }

        if (root.TryGetValue("rest_send_rate_limit_per_sec", out var rs))
        {
            cfg.RestSendRateLimitPerSec = RequireRate("rest_send_rate_limit_per_sec", rs);
        }

        if (root.TryGetValue("max_workers", out var mw))
        {
            cfg.MaxWorkers = ToInt(mw, cfg.MaxWorkers);
        }

        if (root.TryGetValue("max_connections_per_principal", out var mcp))
        {
            cfg.MaxConnectionsPerPrincipal = ToInt(mcp, cfg.MaxConnectionsPerPrincipal);
        }

        if (root.TryGetValue("worker_frame_on_invalid", out var wfi) && wfi is string wfs)
        {
            cfg.WorkerFrameOnInvalid = wfs;
        }

        if (root.TryGetValue("server", out var serverObj) && serverObj is TomlTable server)
        {
            ApplyServer(cfg.Server, server);
        }

        if (root.TryGetValue("auth", out var authObj) && authObj is TomlTable auth)
        {
            ApplyAuth(cfg.Auth, auth);
        }

        if (root.TryGetValue("control_plane", out var cpObj) && cpObj is TomlTable cp)
        {
            if (cp.TryGetValue("backend", out var b) && b is string bs) cfg.ControlPlane.Backend = bs;
            if (cp.TryGetValue("database_url", out var du) && du is string dus) cfg.ControlPlane.DatabaseUrl = dus;
        }

        if (root.TryGetValue("security", out var secObj) && secObj is TomlTable sec)
        {
            if (sec.TryGetValue("mode", out var m) && m is string ms) cfg.Security.Mode = ms;
            if (sec.TryGetValue("metrics_require_auth", out var mra) && mra is bool mrb) cfg.Security.MetricsRequireAuth = mrb;
            if (sec.TryGetValue("default_session_visibility", out var dsv) && dsv is string dss) cfg.Security.DefaultSessionVisibility = dss;
        }

        if (root.TryGetValue("governance", out var govObj) && govObj is TomlTable gov)
        {
            ApplyGovernance(cfg.Governance, gov);
        }

        if (root.TryGetValue("sessions", out var sessObj) && sessObj is TomlTableArray sessions)
        {
            cfg.Sessions = new List<SessionDefinition>();
            foreach (var item in sessions)
            {
                if (item is not TomlTable t) continue;
                cfg.Sessions.Add(SessionLoader.FromTable(t));
            }
        }

        if (root.TryGetValue("graphical_targets", out var gtObj) && gtObj is TomlTableArray targets)
        {
            cfg.GraphicalTargets = new List<GraphicalTargetDefinition>();
            foreach (var item in targets)
            {
                if (item is not TomlTable t) continue;
                var def = new GraphicalTargetDefinition
                {
                    TargetId = t.TryGetValue("target_id", out var tid) && tid is string sTid && !string.IsNullOrWhiteSpace(sTid)
                        ? sTid
                        : "gt-" + Guid.NewGuid().ToString("N")[..12],
                    TenantId = t.TryGetValue("tenant_id", out var tenant) && tenant is string sTenant ? sTenant : "",
                    Protocol = t.TryGetValue("protocol", out var p) && p is string sProto ? sProto : "rfb",
                    TargetAddress = t.TryGetValue("target_address", out var ta) && ta is string sTa ? sTa : "",
                    VmName = t.TryGetValue("vm_name", out var vm) && vm is string sVm ? sVm : null,
                    Name = t.TryGetValue("name", out var n) && n is string sN ? sN : "",
                    Description = t.TryGetValue("description", out var d) && d is string sD ? sD : null,
                    Enabled = !(t.TryGetValue("enabled", out var e) && e is bool be) || be,
                    Width = t.TryGetValue("width", out var w) ? ToInt(w, 640) : 640,
                    Height = t.TryGetValue("height", out var h) ? ToInt(h, 480) : 480,
                    IsStatic = t.TryGetValue("is_static", out var isStatic) && isStatic is bool bs && bs,
                };

                if (t.TryGetValue("config", out var cfgObj) && cfgObj is TomlTable cfgTable)
                {
                    def.Config = cfgTable.ToDictionary(kv => kv.Key, kv => (object?)kv.Value);
                }

                cfg.GraphicalTargets.Add(def);
            }
        }
    }

    private static void ApplyServer(ServerBindConfig s, TomlTable t)
    {
        if (t.TryGetValue("host", out var h) && h is string hs) s.Host = hs;
        if (t.TryGetValue("port", out var p)) s.Port = ToInt(p, s.Port);
        if (t.TryGetValue("public_base_url", out var u) && u is string us) s.PublicBaseUrl = us;
        if (t.TryGetValue("title", out var title) && title is string ts) s.Title = ts;
        if (t.TryGetValue("node_id", out var n) && n is string ns) s.NodeId = ns;
        if (t.TryGetValue("allowed_origins", out var ao) && ao is TomlArray arr)
        {
            s.AllowedOrigins = arr.OfType<string>().ToList();
        }
    }

    private static void ApplyAuth(AuthConfig a, TomlTable t)
    {
        if (t.TryGetValue("mode", out var m) && m is string ms) a.Mode = ms;
        if (t.TryGetValue("principal_header", out var ph) && ph is string phs) a.PrincipalHeader = phs;
        if (t.TryGetValue("tenant_header", out var tnh) && tnh is string tnhs) a.TenantHeader = tnhs;
        if (t.TryGetValue("role_header", out var rh) && rh is string rhs) a.RoleHeader = rhs;
        if (t.TryGetValue("tenant_cookie", out var tc) && tc is string tcs) a.TenantCookie = tcs;
        if (t.TryGetValue("jwt_issuer", out var ji) && ji is string jis) a.JwtIssuer = jis;
        if (t.TryGetValue("jwt_audience", out var ja) && ja is string jas) a.JwtAudience = jas;
        if (t.TryGetValue("jwt_public_key_pem", out var pem) && pem is string pems) a.JwtPublicKeyPem = pems;
        if (t.TryGetValue("jwt_jwks_url", out var jwks) && jwks is string jwkss) a.JwtJwksUrl = jwkss;
        if (t.TryGetValue("worker_bearer_token", out var wt) && wt is string wts) a.WorkerBearerToken = wts;
        if (t.TryGetValue("api_keys_enabled", out var apiKeysEn) && apiKeysEn is bool apiKeysEnB) a.ApiKeysEnabled = apiKeysEnB;
        if (t.TryGetValue("header_mode_acknowledged", out var hma) && hma is bool hmab) a.HeaderModeAcknowledged = hmab;
        if (t.TryGetValue("jwt_tenant_claim", out var tcl) && tcl is string tcls) a.JWTTenantClaim = tcls;
        if (t.TryGetValue("clock_skew_seconds", out var cs)) a.ClockSkewSeconds = ToInt(cs, a.ClockSkewSeconds);
        if (t.TryGetValue("trusted_proxy_ips", out var tpi) && tpi is TomlArray arr)
        {
            a.TrustedProxyIps = arr.OfType<string>().ToList();
        }

        if (t.TryGetValue("jwt_algorithms", out var algs) && algs is TomlArray aarr)
        {
            a.JwtAlgorithms = aarr.OfType<string>().ToList();
        }

        if (t.TryGetValue("jwt_default_role", out var jdr) && jdr is string jdrs) a.JwtDefaultRole = jdrs;
        if (t.TryGetValue("cf_access_team_domain", out var team) && team is string teams) a.CfAccessTeamDomain = teams;
        ApplyCfAccessTeamDomain(a);
    }

    /// <summary>
    /// Fills empty JwtJwksUrl / JwtIssuer from a Cloudflare Access team domain.
    /// Explicit operator values always win. Default JwtIssuer "provide-uterm" is
    /// non-empty, so operators must clear jwt_issuer for team-domain issuer fill
    /// (same as Go).
    /// </summary>
    internal static void ApplyCfAccessTeamDomain(AuthConfig a)
    {
        var team = (a.CfAccessTeamDomain ?? "").Trim();
        if (team.Length == 0) return;

        if (team.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            team = team["https://".Length..];
        else if (team.StartsWith("http://", StringComparison.OrdinalIgnoreCase))
            team = team["http://".Length..];

        var slash = team.IndexOf('/');
        if (slash >= 0) team = team[..slash];
        if (team.EndsWith(".cloudflareaccess.com", StringComparison.OrdinalIgnoreCase))
            team = team[..^".cloudflareaccess.com".Length];

        team = team.Trim();
        if (team.Length == 0) return;

        if (string.IsNullOrWhiteSpace(a.JwtJwksUrl))
            a.JwtJwksUrl = $"https://{team}.cloudflareaccess.com/cdn-cgi/access/certs";

        if (string.IsNullOrWhiteSpace(a.JwtIssuer))
            a.JwtIssuer = $"https://{team}.cloudflareaccess.com";
    }

    private static void ApplyGovernance(GovernanceConfig g, TomlTable t)
    {
        if (t.TryGetValue("authz_webhook_url", out var url) && url is string urls) g.AuthzWebhookUrl = urls;
        if (t.TryGetValue("authz_webhook_secret", out var secret) && secret is string secrets) g.AuthzWebhookSecret = secrets;
        if (t.TryGetValue("authz_webhook_timeout_s", out var timeout)) g.AuthzWebhookTimeoutS = ToDouble(timeout, g.AuthzWebhookTimeoutS);
    }

    private static int ToInt(object? v, int fallback) => v switch
    {
        long l => (int)l,
        int i => i,
        double d => (int)d,
        string s when int.TryParse(s, out var n) => n,
        _ => fallback,
    };

    /// <summary>
    /// A written-down rate must be a number. TOML integers and floats both
    /// count (an operator who writes <c>7</c> rather than <c>7.0</c> means 7),
    /// as does a numeric string, matching the reference's lax float field;
    /// anything else is refused by name rather than folded to the default.
    /// </summary>
    private static double RequireRate(string key, object? v) => v switch
    {
        double d => d,
        long l => l,
        int i => i,
        string s when double.TryParse(
            s, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var n)
            => n,
        _ => throw new ArgumentException($"{key} must be a number, got: {v}"),
    };

    private static double ToDouble(object? v, double fallback) => v switch
    {
        double d => d,
        long l => l,
        int i => i,
        string s when double.TryParse(s, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var n) => n,
        _ => fallback,
    };
}
