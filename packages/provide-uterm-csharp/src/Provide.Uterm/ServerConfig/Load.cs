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

    private static void ApplyToml(UtermServerConfig cfg, TomlTable root)
    {
        if (root.TryGetValue("environment", out var env) && env is string es)
        {
            cfg.Environment = es;
        }

        if (root.TryGetValue("browser_rate_limit_per_sec", out var br) && br is double brd)
        {
            cfg.BrowserRateLimitPerSec = brd;
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

        if (root.TryGetValue("sessions", out var sessObj) && sessObj is TomlTableArray sessions)
        {
            cfg.Sessions = new List<SessionDefinition>();
            foreach (var item in sessions)
            {
                if (item is not TomlTable t) continue;
                var def = new SessionDefinition();
                if (t.TryGetValue("session_id", out var sid) && sid is string sids) def.SessionId = sids;
                if (t.TryGetValue("display_name", out var dn) && dn is string dns) def.DisplayName = dns;
                if (t.TryGetValue("connector_type", out var ct) && ct is string cts) def.ConnectorType = cts;
                if (t.TryGetValue("visibility", out var vis) && vis is string viss) def.Visibility = viss;
                if (t.TryGetValue("owner", out var ow) && ow is string ows) def.Owner = ows;
                cfg.Sessions.Add(def);
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
        if (t.TryGetValue("role_header", out var rh) && rh is string rhs) a.RoleHeader = rhs;
        if (t.TryGetValue("jwt_issuer", out var ji) && ji is string jis) a.JwtIssuer = jis;
        if (t.TryGetValue("jwt_audience", out var ja) && ja is string jas) a.JwtAudience = jas;
        if (t.TryGetValue("jwt_public_key_pem", out var pem) && pem is string pems) a.JwtPublicKeyPem = pems;
        if (t.TryGetValue("jwt_jwks_url", out var jwks) && jwks is string jwkss) a.JwtJwksUrl = jwkss;
        if (t.TryGetValue("worker_bearer_token", out var wt) && wt is string wts) a.WorkerBearerToken = wts;
        if (t.TryGetValue("api_keys_enabled", out var apiKeysEn) && apiKeysEn is bool apiKeysEnB) a.ApiKeysEnabled = apiKeysEnB;
        if (t.TryGetValue("header_mode_acknowledged", out var hma) && hma is bool hmab) a.HeaderModeAcknowledged = hmab;
        if (t.TryGetValue("clock_skew_seconds", out var cs)) a.ClockSkewSeconds = ToInt(cs, a.ClockSkewSeconds);
        if (t.TryGetValue("trusted_proxy_ips", out var tpi) && tpi is TomlArray arr)
        {
            a.TrustedProxyIps = arr.OfType<string>().ToList();
        }

        if (t.TryGetValue("jwt_algorithms", out var algs) && algs is TomlArray aarr)
        {
            a.JwtAlgorithms = aarr.OfType<string>().ToList();
        }
    }

    private static int ToInt(object? v, int fallback) => v switch
    {
        long l => (int)l,
        int i => i,
        double d => (int)d,
        string s when int.TryParse(s, out var n) => n,
        _ => fallback,
    };
}
