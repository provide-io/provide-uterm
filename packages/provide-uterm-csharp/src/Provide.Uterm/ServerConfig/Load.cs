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
        // Tomlyn's Deserialize is typed as nullable. An empty file deserializes
        // to an empty table rather than null, so the fallback is equivalent to
        // "no keys set" and leaves every default in place.
        var model = TomlSerializer.Deserialize<TomlTable>(text) ?? new TomlTable();
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
        "fanout_allow_unknown_members",
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
    /// <summary>
    /// Which reference model validates each TOML section, so a test can tie the
    /// key sets below to the recorded schema rather than to a transcription.
    /// </summary>
    internal static IReadOnlyDictionary<string, string> SectionModelsForTests { get; } =
        new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["server"] = "ServerBindConfig",
        ["auth"] = "AuthConfig",
        ["control_plane"] = "ControlPlaneConfig",
        ["ui"] = "UiConfig",
        ["recording"] = "RecordingConfig",
        ["profiles"] = "ProfileStoreConfig",
        ["security"] = "SecurityConfig",
        ["tunnel"] = "TunnelConfig",
        ["webhooks"] = "WebhooksConfig",
        ["pam"] = "PamConfig",
        ["governance"] = "GovernanceConfig",
        ["audit"] = "AuditConfig",
        ["graphical_targets"] = "GraphicalTargetConfig",
        ["sessions"] = "SessionDefinition",
    };

    /// <summary>
    /// Field names the reference accepts inside each section.
    /// </summary>
    /// <remarks>
    /// The reference gets this from one place — <c>extra="forbid"</c> on
    /// <c>ServerBaseModel</c>, which every section model inherits — so a typo one
    /// line inside <c>[server]</c> is refused exactly like a bad top-level key.
    /// This port refused only the top level, and a section is where the
    /// security-relevant keys live.
    ///
    /// These are the <em>reference's</em> names rather than this port's, for the
    /// same reason the top-level set is: one server.toml should be readable by
    /// any port, so a section C# does not model is recognised and its contents
    /// still validated.
    ///
    /// Generated from <c>configschema_golden.json</c> rather than typed out, and
    /// held to it by a test. A hand-copied set rots, and a stale one would refuse
    /// a key the reference accepts — which breaks a working deployment on
    /// upgrade, a worse failure than the silence it replaces.
    /// </remarks>
    internal static IReadOnlyDictionary<string, HashSet<string>> KnownNestedKeysForTests => KnownNestedKeys;

    private static readonly Dictionary<string, HashSet<string>> KnownNestedKeys = new(StringComparer.Ordinal)
    {
        ["server"] = new(StringComparer.Ordinal)
        {
            "allowed_origins", "host", "max_sessions", "node_id", "port", "public_base_url",
            "title"
        },
        ["auth"] = new(StringComparer.Ordinal)
        {
            "allow_adhoc_browser_observers", "api_keys_enabled", "clock_skew_seconds",
            "delegate_roles", "header_mode_acknowledged", "identity_provider",
            "jwt_algorithms", "jwt_audience", "jwt_issuer", "jwt_jwks_url",
            "jwt_public_key_pem", "jwt_roles_claim", "jwt_scopes_claim", "jwt_tenant_claim",
            "mode", "principal_cookie", "principal_header", "require_jwt_in_production",
            "require_upstream_proxy_secret", "role_cookie", "role_header", "surface_cookie",
            "tenant_cookie", "tenant_header", "token_cookie", "trusted_proxy_ips",
            "upstream_proxy_secret", "jwt_default_role", "cf_access_team_domain",
            "webhook_idp_forward_cookies",
            "webhook_idp_forward_headers", "webhook_idp_on_failure",
            "webhook_idp_require_response_nonce", "webhook_idp_require_signed_response",
            "webhook_idp_secret", "webhook_idp_timeout_s", "webhook_idp_url",
            "worker_bearer_token"
        },
        ["control_plane"] = new(StringComparer.Ordinal)
        {
            "backend", "database_url", "reap_interval_s", "reap_retention_s"
        },
        ["ui"] = new(StringComparer.Ordinal)
        {
            "app_path", "assets_path", "fitaddon_cdn", "fitaddon_cdn_integrity", "fonts_cdn",
            "xterm_cdn", "xterm_cdn_integrity"
        },
        ["recording"] = new(StringComparer.Ordinal)
        {
            "control_channel_mode", "directory", "enabled_by_default", "flush_batch_size",
            "flush_interval_s", "max_bytes", "redact_sensitive", "retention_s", "store_type",
            "webhook_secret", "webhook_timeout_s", "webhook_url"
        },
        ["profiles"] = new(StringComparer.Ordinal)
        {
            "directory"
        },
        ["security"] = new(StringComparer.Ordinal)
        {
            "block_private_connector_targets", "csp", "default_session_visibility",
            "dev_mode_acknowledged", "hsts", "metrics_require_auth", "mode",
            "permissions_policy", "referrer_policy", "x_content_type_options",
            "x_frame_options"
        },
        ["tunnel"] = new(StringComparer.Ordinal)
        {
            "cookie_samesite", "cookie_secure", "ip_binding", "token_transport", "token_ttl_s"
        },
        ["webhooks"] = new(StringComparer.Ordinal)
        {
            "allow_loopback_destinations"
        },
        ["pam"] = new(StringComparer.Ordinal)
        {
            "auto_session", "auto_session_command", "capture_socket_dir", "mode",
            "notify_socket", "relay_token", "relay_url", "require_peer_uids"
        },
        ["governance"] = new(StringComparer.Ordinal)
        {
            "authz_webhook_secret", "authz_webhook_timeout_s", "authz_webhook_url",
            "behavioral_audit_interval_s", "behavioral_audit_secret", "behavioral_audit_url",
            "behavioral_fail_open", "behavioral_max_cps", "behavioral_min_jitter",
            "discovery_provider", "external_connectors", "policy_webhook_secret",
            "policy_webhook_timeout_s", "policy_webhook_url", "registry_webhook_interval_s",
            "registry_webhook_secret", "registry_webhook_url", "telemetry_webhook_secret",
            "telemetry_webhook_timeout_s", "telemetry_webhook_url"
        },
        ["audit"] = new(StringComparer.Ordinal)
        {
            "chain_enabled", "chain_file"
        },
        ["graphical_targets"] = new(StringComparer.Ordinal)
        {
            "config", "description", "enabled", "height", "is_static", "name", "protocol",
            "target_address", "target_id", "tenant_id", "vm_name", "width"
        },
        ["sessions"] = new(StringComparer.Ordinal)
        {
            "auto_start", "auto_transfer_idle_s", "connector_config", "connector_type",
            "created_at", "display_name", "ephemeral", "input_mode", "keystroke_queue",
            "owner", "presence", "recording_enabled", "session_id", "tags", "visibility"
        },
    };

    /// <summary>
    /// Sections whose unrecognised keys are folded rather than refused.
    /// </summary>
    /// <remarks>
    /// <c>[[sessions]]</c> only. The reference's own before-validator folds
    /// unknown keys into <c>connector_config</c>, because a connector's options
    /// are open-ended by design and have to reach it somehow. Everything else,
    /// including the sibling list <c>[[graphical_targets]]</c>, is validated.
    /// </remarks>
    private static readonly HashSet<string> OpenEndedSections = new(StringComparer.Ordinal) { "sessions" };

    private static void RefuseUnknownNestedKeys(TomlTable root)
    {
        foreach (var section in root.Keys)
        {
            if (OpenEndedSections.Contains(section)) continue;
            if (!KnownNestedKeys.TryGetValue(section, out var known)) continue;

            foreach (var table in TablesIn(root, section))
            {
                foreach (var key in table.Keys)
                {
                    if (known.Contains(key)) continue;
                    // Named with its section: `hsot` alone would send an operator
                    // hunting through the file. The reference's own formatter
                    // drops the location, which is a shortcoming worth not
                    // copying.
                    throw new ArgumentException($"{section}.{key}: Extra inputs are not permitted");
                }
            }
        }
    }

    /// <summary>Every table a section holds — one for a table, many for an array of them.</summary>
    private static IEnumerable<TomlTable> TablesIn(TomlTable root, string section)
    {
        var value = root[section];
        if (value is TomlTable table)
        {
            yield return table;
        }
        else if (value is TomlTableArray array)
        {
            foreach (var entry in array)
            {
                yield return entry;
            }
        }
    }

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
        // Sections after the top level, and in that order: a file with both a bad
        // section name and a bad key inside another should complain about the
        // section, which is the larger mistake.
        RefuseUnknownNestedKeys(root);

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

        if (root.TryGetValue("fanout_allow_unknown_members", out var fanoutUnknown) && fanoutUnknown is bool allowUnknown)
        {
            cfg.FanoutAllowUnknownMembers = allowUnknown;
        }

        if (root.TryGetValue("server", out var serverObj) && serverObj is TomlTable server)
        {
            ApplyServer(cfg.Server, server);
        }

        if (root.TryGetValue("auth", out var authObj) && authObj is TomlTable auth)
        {
            ApplyAuth(cfg.Auth, auth);
        }

        if (root.TryGetValue("ui", out var uiObj) && uiObj is TomlTable ui)
        {
            ApplyUi(cfg.Ui, ui);
        }

        if (root.TryGetValue("recording", out var recObj) && recObj is TomlTable rec)
        {
            ApplyRecording(cfg.Recording, rec);
        }

        if (root.TryGetValue("tunnel", out var tunObj) && tunObj is TomlTable tun)
        {
            ApplyTunnel(cfg.Tunnel, tun);
        }

        if (root.TryGetValue("control_plane", out var cpObj) && cpObj is TomlTable cp)
        {
            if (cp.TryGetValue("backend", out var b) && b is string bs) cfg.ControlPlane.Backend = bs;
            if (cp.TryGetValue("database_url", out var du) && du is string dus) cfg.ControlPlane.DatabaseUrl = dus;
            if (cp.TryGetValue("reap_interval_s", out var ri)) cfg.ControlPlane.ReapIntervalS = ToInt(ri, cfg.ControlPlane.ReapIntervalS);
            if (cp.TryGetValue("reap_retention_s", out var rr)) cfg.ControlPlane.ReapRetentionS = ToInt(rr, cfg.ControlPlane.ReapRetentionS);
        }

        if (root.TryGetValue("security", out var secObj) && secObj is TomlTable sec)
        {
            if (sec.TryGetValue("mode", out var m) && m is string ms) cfg.Security.Mode = ms;
            if (sec.TryGetValue("metrics_require_auth", out var mra) && mra is bool mrb) cfg.Security.MetricsRequireAuth = mrb;
            if (sec.TryGetValue("default_session_visibility", out var dsv) && dsv is string dss) cfg.Security.DefaultSessionVisibility = dss;
            if (sec.TryGetValue("dev_mode_acknowledged", out var dma) && dma is bool dmab) cfg.Security.DevModeAcknowledged = dmab;
            // The SSRF guard on connector and VNC dial-out targets. It had a
            // property, three readers, and no way in from a file.
            if (sec.TryGetValue("block_private_connector_targets", out var bpc) && bpc is bool bpcb)
            {
                cfg.Security.BlockPrivateConnectorTargets = bpcb;
            }
        }

        if (root.TryGetValue("governance", out var govObj) && govObj is TomlTable gov)
        {
            ApplyGovernance(cfg.Governance, gov);
        }

        if (root.TryGetValue("webhooks", out var whObj) && whObj is TomlTable wh
            && wh.TryGetValue("allow_loopback_destinations", out var loopback) && loopback is bool loopbackB)
        {
            cfg.Webhooks.AllowLoopbackDestinations = loopbackB;
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
        if (t.TryGetValue("max_sessions", out var maxSessions)) s.MaxSessions = ToInt(maxSessions, s.MaxSessions ?? 0);
        if (t.TryGetValue("allowed_origins", out var ao) && ao is TomlArray arr)
        {
            s.AllowedOrigins = arr.OfType<string>().ToList();
        }
    }

    /// <summary>
    /// The <c>[ui]</c> section, which was never read at all.
    /// </summary>
    /// <remarks>
    /// Six keys with live readers in <c>UtermServer.StaticUi</c> — the two served
    /// paths and the xterm/fit-addon CDN bases with their SRI hashes. An operator
    /// pinning an integrity hash got the default unpinned CDN and no warning,
    /// which is the failure mode SRI exists to prevent.
    /// </remarks>
    private static void ApplyUi(UiConfig ui, TomlTable t)
    {
        if (t.TryGetValue("app_path", out var app) && app is string apps) ui.AppPath = apps;
        if (t.TryGetValue("assets_path", out var assets) && assets is string assetss) ui.AssetsPath = assetss;
        if (t.TryGetValue("xterm_cdn", out var xc) && xc is string xcs) ui.XtermCdn = xcs;
        if (t.TryGetValue("xterm_cdn_integrity", out var xi) && xi is string xis) ui.XtermCdnIntegrity = xis;
        if (t.TryGetValue("fitaddon_cdn", out var fc) && fc is string fcs) ui.FitAddonCdn = fcs;
        if (t.TryGetValue("fitaddon_cdn_integrity", out var fi) && fi is string fis) ui.FitAddonCdnIntegrity = fis;
    }

    /// <summary>
    /// The <c>[recording]</c> section, which was never read at all.
    /// </summary>
    /// <remarks>
    /// <c>enabled_by_default</c>, <c>directory</c> and <c>store_type</c> have
    /// readers in the hosted factory, so a deployment that asked to record wrote
    /// nothing, and one that asked not to could not say so.
    /// </remarks>
    private static void ApplyRecording(RecordingConfig r, TomlTable t)
    {
        if (t.TryGetValue("enabled_by_default", out var en) && en is bool enb) r.EnabledByDefault = enb;
        if (t.TryGetValue("directory", out var dir) && dir is string dirs) r.Directory = dirs;
        if (t.TryGetValue("control_channel_mode", out var ccm) && ccm is string ccms) r.ControlChannelMode = ccms;
        if (t.TryGetValue("redact_sensitive", out var rs) && rs is bool rsb) r.RedactSensitive = rsb;
        if (t.TryGetValue("store_type", out var st) && st is string sts) r.StoreType = sts;
    }

    /// <summary>
    /// The <c>[tunnel]</c> section, which was never read at all.
    /// </summary>
    /// <remarks>
    /// The share-link security knobs: <c>cookie_secure</c>, <c>cookie_samesite</c>
    /// and <c>ip_binding</c> all have readers in <c>Tunnel.cs</c>, so an operator
    /// hardening a public share got the defaults regardless of what they wrote.
    /// </remarks>
    private static void ApplyTunnel(TunnelConfig tunnel, TomlTable t)
    {
        if (t.TryGetValue("token_ttl_s", out var ttl)) tunnel.TokenTtlS = ToInt(ttl, tunnel.TokenTtlS);
        if (t.TryGetValue("token_transport", out var tt) && tt is string tts) tunnel.TokenTransport = tts;
        if (t.TryGetValue("cookie_secure", out var csec) && csec is bool csecb) tunnel.CookieSecure = csecb;
        if (t.TryGetValue("cookie_samesite", out var css) && css is string csss) tunnel.CookieSamesite = csss;
        if (t.TryGetValue("ip_binding", out var ipb) && ipb is bool ipbb) tunnel.IpBinding = ipbb;
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

        // The cookie names header/cookie auth actually reads (LocalIdentity), and
        // the JWT claim names that decide roles and scopes. All had properties and
        // readers; none had a way in from a file, so a deployment whose IdP uses
        // "groups" instead of "roles" resolved no roles at all and every operator
        // was silently demoted to viewer.
        if (t.TryGetValue("principal_cookie", out var pc) && pc is string pcs) a.PrincipalCookie = pcs;
        if (t.TryGetValue("role_cookie", out var rc) && rc is string rcs) a.RoleCookie = rcs;
        if (t.TryGetValue("surface_cookie", out var sc) && sc is string scs) a.SurfaceCookie = scs;
        if (t.TryGetValue("token_cookie", out var tokc) && tokc is string tokcs) a.TokenCookie = tokcs;
        if (t.TryGetValue("jwt_roles_claim", out var jrc) && jrc is string jrcs) a.JwtRolesClaim = jrcs;
        if (t.TryGetValue("jwt_scopes_claim", out var jsc) && jsc is string jscs) a.JwtScopesClaim = jscs;
        // The delegated-IdP group. No consumer in this port yet, so binding them
        // does not switch anything on — but a discarded key and an unimplemented
        // feature are different failures, and only the second one is honest.
        if (t.TryGetValue("identity_provider", out var ip) && ip is string ips) a.IdentityProvider = ips;
        if (t.TryGetValue("delegate_roles", out var dr) && dr is bool drb) a.DelegateRoles = drb;
        if (t.TryGetValue("webhook_idp_url", out var wiu) && wiu is string wius) a.WebhookIdpUrl = wius;
        if (t.TryGetValue("webhook_idp_secret", out var wis) && wis is string wiss) a.WebhookIdpSecret = wiss;
        if (t.TryGetValue("webhook_idp_timeout_s", out var wit)) a.WebhookIdpTimeoutS = ToDouble(wit, a.WebhookIdpTimeoutS);
        if (t.TryGetValue("webhook_idp_on_failure", out var wif) && wif is string wifs) a.WebhookIdpOnFailure = wifs;
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
        if (t.TryGetValue("policy_webhook_url", out var policyUrl) && policyUrl is string policyUrls) g.PolicyWebhookUrl = policyUrls;
        if (t.TryGetValue("policy_webhook_secret", out var policySecret) && policySecret is string policySecrets) g.PolicyWebhookSecret = policySecrets;
        if (t.TryGetValue("policy_webhook_timeout_s", out var policyTimeout)) g.PolicyWebhookTimeoutS = ToDouble(policyTimeout, g.PolicyWebhookTimeoutS);
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
