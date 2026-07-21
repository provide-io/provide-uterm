//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests.Server;

public class StaticUiSriTests
{
    [Fact]
    public void FallbackShell_Emits_Sri_When_Configured()
    {
        var cfg = UtermServerConfig.Default();
        cfg.Ui.XtermCdn = "https://cdn.jsdelivr.net/npm/@xterm/xterm@6.0.0";
        cfg.Ui.XtermCdnIntegrity = "sha384-testcss";
        cfg.Ui.FitAddonCdn = "https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.11.0";
        cfg.Ui.FitAddonCdnIntegrity = "sha384-testfit";
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "test",
        });

        var html = server.BuildFallbackShellHtml("inspect/demo");
        Assert.Contains("integrity=\"sha384-testcss\"", html, StringComparison.Ordinal);
        Assert.Contains("crossorigin=\"anonymous\"", html, StringComparison.Ordinal);
        Assert.Contains("xterm.css", html, StringComparison.Ordinal);
        Assert.Contains("integrity=\"sha384-testfit\"", html, StringComparison.Ordinal);
        Assert.Contains("addon-fit.js", html, StringComparison.Ordinal);
    }

    [Fact]
    public void FallbackShell_Omits_Integrity_Without_Config()
    {
        var cfg = UtermServerConfig.Default();
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "test",
        });
        var html = server.BuildFallbackShellHtml("");
        Assert.DoesNotContain("integrity=", html, StringComparison.Ordinal);
    }
}
