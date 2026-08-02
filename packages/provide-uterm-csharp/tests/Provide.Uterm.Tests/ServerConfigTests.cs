//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

public class ServerConfigTests
{
    [Fact]
    public void Default_UsesDevFriendlyAuthWhenFactoryDefault()
    {
        var cfg = UtermServerConfig.Default();
        Assert.Equal("dev_token", cfg.Auth.Mode);
        Assert.False(string.IsNullOrEmpty(cfg.Server.PublicBaseUrl));
    }

    [Fact]
    public void Load_EmptyPath_ReturnsDefaults()
    {
        var cfg = ConfigLoader.Load(null);
        Assert.Equal(8780, cfg.Server.Port);
    }

    [Fact]
    public void Load_FromToml_BindsMaxSessions()
    {
        // Its own row: `[server] max_sessions` is bound nowhere else, so a
        // dropped assignment here would be invisible to every other test in
        // this file.
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, """
            [server]
            max_sessions = 42
            """);
        try
        {
            var cfg = ConfigLoader.Load(tmp);
            Assert.Equal(42, cfg.Server.MaxSessions);
        }
        finally
        {
            File.Delete(tmp);
        }
    }

    [Fact]
    public void Load_FromToml_BindsJwtDefaultRole()
    {
        // `jwt_default_role` used to be unreachable through the public API:
        // ApplyAuth bound it, but KnownNestedKeys["auth"] didn't list it, so
        // ConfigLoader.Load refused the TOML outright ("Extra inputs are not
        // permitted") before ApplyAuth ever ran. Now allow-listed as a
        // documented port-only extension (see ConfigLoader.PortOnlyKeysForTests)
        // — this exercises the real load path, not a reflection workaround.
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, """
            [auth]
            jwt_default_role = "operator"
            """);
        try
        {
            var cfg = ConfigLoader.Load(tmp);
            Assert.Equal("operator", cfg.Auth.JwtDefaultRole);
        }
        finally
        {
            File.Delete(tmp);
        }
    }

    [Fact]
    public void Load_FromToml_BindsCfAccessTeamDomainAndAppliesItsFill()
    {
        // Same reachability fix as Load_FromToml_BindsJwtDefaultRole above.
        // Also pins that ApplyAuth's *unconditional* call to
        // ApplyCfAccessTeamDomain (run for every AuthConfig whether or not the
        // key was set) actually does something once the field it reads is
        // non-null — not just that it runs without throwing.
        // jwt_issuer must be explicitly cleared: its non-empty default
        // ("provide-uterm") otherwise wins over the team-domain fill (operator
        // values always take precedence — see ApplyCfAccessTeamDomain's
        // docstring, same rule as Go).
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, """
            [auth]
            cf_access_team_domain = "myteam"
            jwt_issuer = ""
            """);
        try
        {
            var cfg = ConfigLoader.Load(tmp);
            Assert.Equal("myteam", cfg.Auth.CfAccessTeamDomain);
            Assert.Equal("https://myteam.cloudflareaccess.com/cdn-cgi/access/certs", cfg.Auth.JwtJwksUrl);
            Assert.Equal("https://myteam.cloudflareaccess.com", cfg.Auth.JwtIssuer);
        }
        finally
        {
            File.Delete(tmp);
        }
    }

    [Fact]
    public void Load_FromToml_OverridesHostPort()
    {
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, """
            [server]
            host = "0.0.0.0"
            port = 9999
            public_base_url = ""
            allowed_origins = ["http://x"]

            [auth]
            mode = "jwt"
            """);
        try
        {
            var cfg = ConfigLoader.Load(tmp);
            Assert.Equal("0.0.0.0", cfg.Server.Host);
            Assert.Equal(9999, cfg.Server.Port);
            Assert.Equal("jwt", cfg.Auth.Mode);
            Assert.Equal("x-uterm-tenant", cfg.Auth.TenantHeader);
            Assert.Equal("uterm_tenant", cfg.Auth.TenantCookie);
            Assert.Equal("tenant_id", cfg.Auth.JWTTenantClaim);
            // Load derives PublicBaseUrl from the loaded host/port whenever it
            // is left blank (here, explicitly cleared) — a caller that skipped
            // that step would leave it empty rather than "http://0.0.0.0:9999".
            Assert.Equal("http://0.0.0.0:9999", cfg.Server.PublicBaseUrl);
        }
        finally
        {
            File.Delete(tmp);
        }
    }

    [Fact]
    public void Load_FromToml_BindsFanoutAllowUnknownMembers()
    {
        // Its own row here, next to the other top-level scalars, even though
        // ServerFanoutTests already loads this key: that class sits outside the
        // mutation perimeter's test-case-filter, so the loader's only proof
        // that `fanout_allow_unknown_members` is spelled correctly in
        // KnownTopLevelKeys lived somewhere the mutation gate never runs. A
        // mangled entry there does not silently drop the key — it makes
        // RefuseUnknownTopLevelKeys reject the whole file, so a server that
        // configures fanout would refuse to start.
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, "fanout_allow_unknown_members = true\n");
        try
        {
            // False by default, so `true` here cannot pass by accident.
            Assert.False(UtermServerConfig.Default().FanoutAllowUnknownMembers);
            Assert.True(ConfigLoader.Load(tmp).FanoutAllowUnknownMembers);
        }
        finally
        {
            File.Delete(tmp);
        }
    }
}
