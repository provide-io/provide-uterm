//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// What a <c>[[sessions]]</c> entry means, held to the reference:
/// <c>config_schema_session.SessionDefinition</c> (and the Go port of it in
/// <c>serverconfig/session.go</c>).
///
/// A loader that reads only some of a definition's fields is worse than one
/// that refuses it — the operator wrote the field down, the server started,
/// and the setting silently did not apply.
/// </summary>
public class ServerConfigSessionTests
{
    /// <summary>Write TOML to a throwaway file and load it.</summary>
    private static UtermServerConfig LoadToml(string toml)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-sess-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, toml);
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void Tags_Reach_The_Definition_In_Their_Configured_Order()
    {
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "tagged"
            tags = ["ops", "shell", "ops"]
            """);

        var session = Assert.Single(cfg.Sessions);
        // Order and duplicates are the contract: a filter matches on them.
        Assert.Equal(["ops", "shell", "ops"], session.Tags);
    }

    [Fact]
    public void Every_Field_The_Reference_Defines_Is_Read()
    {
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "full"
            display_name = "Full"
            connector_type = "telnet"
            input_mode = "hijack"
            auto_start = false
            tags = ["a"]
            recording_enabled = true
            owner = "alice"
            visibility = "private"
            ephemeral = true
            presence = true
            auto_transfer_idle_s = 90
            keystroke_queue = "replay"

            [sessions.connector_config]
            host = "bbs.example.com"
            port = 23
            """);

        var s = Assert.Single(cfg.Sessions);
        Assert.Equal("full", s.SessionId);
        Assert.Equal("Full", s.DisplayName);
        Assert.Equal("telnet", s.ConnectorType);
        Assert.Equal("hijack", s.InputMode);
        Assert.False(s.AutoStart);
        Assert.Equal(["a"], s.Tags);
        Assert.True(s.RecordingEnabled);
        Assert.Equal("alice", s.Owner);
        Assert.Equal("private", s.Visibility);
        Assert.True(s.Ephemeral);
        Assert.True(s.Presence);
        Assert.Equal(90, s.AutoTransferIdleS);
        Assert.Equal("replay", s.KeystrokeQueue);
        Assert.Equal("bbs.example.com", s.ConnectorConfig["host"]);
        Assert.Equal(23L, s.ConnectorConfig["port"]);
    }

    [Fact]
    public void Omitted_Fields_Take_The_References_Defaults()
    {
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "bare"
            """);

        var s = Assert.Single(cfg.Sessions);
        // display_name defaults to session_id (the before-validator does this).
        Assert.Equal("bare", s.DisplayName);
        Assert.Equal("shell", s.ConnectorType);
        Assert.Equal("open", s.InputMode);
        Assert.True(s.AutoStart);
        Assert.Empty(s.Tags);
        Assert.Null(s.RecordingEnabled);
        Assert.Null(s.Owner);
        Assert.Equal("public", s.Visibility);
        Assert.False(s.Ephemeral);
        Assert.False(s.Presence);
        Assert.Equal(30, s.AutoTransferIdleS);
        Assert.Equal("display", s.KeystrokeQueue);
        Assert.Empty(s.ConnectorConfig);
    }

    [Fact]
    public void Unknown_Keys_Collect_Into_Connector_Config()
    {
        // This is what defeats extra="forbid" for the sessions section: a key
        // the model does not define is not a mistake, it is connector config.
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "ssh-box"
            connector_type = "ssh"
            host = "10.0.0.9"
            port = 2222
            username = "operator"
            """);

        var s = Assert.Single(cfg.Sessions);
        Assert.Equal("10.0.0.9", s.ConnectorConfig["host"]);
        Assert.Equal(2222L, s.ConnectorConfig["port"]);
        Assert.Equal("operator", s.ConnectorConfig["username"]);
    }

    [Fact]
    public void Unknown_Keys_Merge_Over_An_Explicit_Connector_Config()
    {
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "merged"
            connector_type = "websocket"
            extra_top = "from-top"

            [sessions.connector_config]
            url = "wss://example.com/ws"
            """);

        var s = Assert.Single(cfg.Sessions);
        Assert.Equal("wss://example.com/ws", s.ConnectorConfig["url"]);
        Assert.Equal("from-top", s.ConnectorConfig["extra_top"]);
    }

    [Fact]
    public void A_Known_Field_Never_Leaks_Into_Connector_Config()
    {
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "known"
            ephemeral = true
            presence = true
            auto_transfer_idle_s = 5
            keystroke_queue = "replay"
            created_at = "2026-01-01T00:00:00Z"
            """);

        var s = Assert.Single(cfg.Sessions);
        Assert.True(s.Ephemeral);
        Assert.Empty(s.ConnectorConfig);
    }
}
