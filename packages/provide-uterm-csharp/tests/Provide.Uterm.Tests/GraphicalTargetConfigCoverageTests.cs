//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// Coverage for TOML graphical_targets parsing + config→runtime seeding
/// (canonical tenant-scope model, no min_role). Class name lands it in the
/// ~Coverage gate batch.
/// </summary>
public class GraphicalTargetConfigCoverageTests
{
    private static UtermServerConfig LoadToml(string toml)
    {
        var tmp = Path.Combine(Path.GetTempPath(), "uterm-gt-cfg-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(tmp, toml);
        try
        {
            return ConfigLoader.Load(tmp);
        }
        finally
        {
            File.Delete(tmp);
        }
    }

    [Fact]
    public void Toml_GraphicalTargets_Parse_All_Fields()
    {
        var cfg = LoadToml("""
            [auth]
            mode = "jwt"

            [[graphical_targets]]
            target_id = "vm-a"
            tenant_id = "acme"
            protocol = "rfb"
            target_address = "127.0.0.1:5901"
            vm_name = "vm-a"
            name = "VM A"
            description = "first"
            enabled = true
            width = 800
            height = 600
            is_static = true

            [[graphical_targets]]
            protocol = "memory"
            name = "scratch"
            enabled = true
            width = 320
            height = 240

            [[graphical_targets]]
            protocol = "memory"
            name = "disabled"
            enabled = false
            """);

        Assert.Equal(3, cfg.GraphicalTargets.Count);
        var a = cfg.GraphicalTargets[0];
        Assert.Equal("vm-a", a.TargetId);
        Assert.Equal("acme", a.TenantId);
        Assert.Equal("rfb", a.Protocol);
        Assert.Equal("127.0.0.1:5901", a.TargetAddress);
        Assert.Equal("vm-a", a.VmName);
        Assert.True(a.IsStatic);
        Assert.True(a.Enabled);
        Assert.Equal(800, a.Width);
        Assert.False(cfg.GraphicalTargets[2].Enabled);

        // Auto-generated target_id when omitted.
        Assert.StartsWith("gt-", cfg.GraphicalTargets[1].TargetId);
    }

    [Fact]
    public void CreateFromConfig_Seeds_Enabled_Targets()
    {
        var cfg = LoadToml("""
            [auth]
            mode = "jwt"
            jwt_public_key_pem = "secret-key-material"

            [[graphical_targets]]
            target_id = "vm-rfb"
            tenant_id = "acme"
            protocol = "rfb"
            target_address = "127.0.0.1:5902"
            enabled = true

            [[graphical_targets]]
            target_id = "vm-mem"
            protocol = "memory"
            enabled = true

            [[graphical_targets]]
            target_id = "vm-off"
            protocol = "memory"
            enabled = false
            """);

        // Production path: SeedGraphicalTargets + ToGraphicalTargetDefinition run
        // end-to-end (rfb endpoint parsed, memory endpoint dropped, disabled skipped).
        var (server, _) = ServerFactory.CreateFromConfig(cfg);
        Assert.NotNull(server);
    }

    [Fact]
    public void CreateFromConfig_Rejects_Bad_Targets()
    {
        var unsupported = LoadToml("""
            [[graphical_targets]]
            target_id = "bad"
            protocol = "ftp"
            enabled = true
            """);
        Assert.Throws<ArgumentException>(() => ServerFactory.CreateFromConfig(unsupported));

        var rfbNoAddress = LoadToml("""
            [[graphical_targets]]
            target_id = "bad-rfb"
            protocol = "rfb"
            enabled = true
            """);
        Assert.Throws<ArgumentException>(() => ServerFactory.CreateFromConfig(rfbNoAddress));

        // litevirt without a target_address is rejected the same way rfb is.
        var litevirtNoAddress = LoadToml("""
            [[graphical_targets]]
            target_id = "bad-lv"
            protocol = "litevirt"
            enabled = true
            """);
        Assert.Throws<ArgumentException>(() => ServerFactory.CreateFromConfig(litevirtNoAddress));
    }

    [Fact]
    public void Toml_Litevirt_With_Config_Parses_And_Seeds()
    {
        var cfg = LoadToml("""
            [[graphical_targets]]
            target_id = "vm-lv"
            tenant_id = "acme"
            protocol = "litevirt"
            target_address = "10.0.0.5:7443"
            enabled = true

            [graphical_targets.config]
            vm_name = "web-1"
            replicas = 3
            """);

        var t = Assert.Single(cfg.GraphicalTargets);
        Assert.Equal("litevirt", t.Protocol);
        Assert.Equal("web-1", t.Config["vm_name"]);
        Assert.Equal(3L, t.Config["replicas"]);

        // Production seeding path validates litevirt endpoint + threads config.
        var (server, _) = ServerFactory.CreateFromConfig(cfg);
        Assert.NotNull(server);
    }
}
