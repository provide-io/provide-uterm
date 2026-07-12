//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Ansi;
using Provide.Uterm.Manager;
using Provide.Uterm.Recording;
using Provide.Uterm.SessionLogger;

namespace Provide.Uterm.Tests;

/// <summary>
/// Broad smoke tests that drive public surfaces of packages that otherwise sit
/// at 0% coverage. Each test exercises real shipped methods.
/// </summary>
public class SmokeSurfaceTests
{
    [Fact]
    public void AgentManager_SpawnStopRemove()
    {
        var mgr = new AgentManager();
        var a = mgr.Spawn("worker", "agent-1");
        Assert.Equal("agent-1", a.AgentId);
        Assert.Equal("running", a.State);
        Assert.True(mgr.Stop("agent-1"));
        Assert.Equal("stopped", mgr.Get("agent-1")!.State);
        Assert.True(mgr.Remove("agent-1"));
        Assert.Null(mgr.Get("agent-1"));
        var status = mgr.GetSwarmStatus();
        Assert.Equal(0, status["agents"]);
        Assert.NotNull(mgr.GetTimeseriesRecent(10));
        Assert.NotNull(mgr.GetTimeseriesSummary());
    }

    [Fact]
    public async Task SessionLogger_LogAndFlush()
    {
        var dir = Path.Combine(Path.GetTempPath(), "uterm-rec-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            var store = new LocalFileStore(dir);
            await using var logger = new SessionLogger.SessionLogger(store);
            await logger.StartAsync("sess-1");
            await logger.LogAsync("term", new Dictionary<string, object?> { ["data"] = "hi" });
            await logger.FlushAsync();
            await logger.StopAsync();
            Assert.True(Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories).Any());
        }
        finally
        {
            try { Directory.Delete(dir, true); } catch { /* ignore */ }
        }
    }

    [Fact]
    public void Ansi_ConstantsAndUpgrade()
    {
        Assert.NotEmpty(AnsiConstants.DefaultPalette);
        Assert.NotEmpty(AnsiConstants.ClearScreen);
        var upgraded = Upgrade.UpgradeTo256("\u001b[31mred\u001b[0m");
        Assert.Contains("\u001b[", upgraded, StringComparison.Ordinal);
        var truec = Upgrade.UpgradeToTruecolor("\u001b[31mred\u001b[0m");
        Assert.Contains("\u001b[", truec, StringComparison.Ordinal);
        var dialect = "test-d-" + Guid.NewGuid().ToString("N")[..8];
        Assert.Null(ColorDialectRegistry.RegisterColorDialect(dialect, s => s.Replace("X", "Y", StringComparison.Ordinal)));
        Assert.Contains(dialect, ColorDialectRegistry.RegisteredDialects());
        Assert.Equal("Y", ColorDialectRegistry.NormalizeColors("X"));
        Assert.Null(ColorDialectRegistry.UnregisterColorDialect(dialect));
    }

    [Fact]
    public void ManagerHost_Help_ExitsZero()
    {
        var code = ManagerHost.Run(new[] { "--help" });
        Assert.Equal(0, code);
    }

}
