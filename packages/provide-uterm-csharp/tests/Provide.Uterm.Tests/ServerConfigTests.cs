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
    public void Load_FromToml_OverridesHostPort()
    {
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, """
            [server]
            host = "0.0.0.0"
            port = 9999
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
        }
        finally
        {
            File.Delete(tmp);
        }
    }
}
