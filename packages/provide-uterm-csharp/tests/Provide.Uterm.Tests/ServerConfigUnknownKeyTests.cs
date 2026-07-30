//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// A key nobody recognises is a mistake, and a mistake in a config file has to
/// be said out loud.
///
/// The reference forbids extras structurally: every server model derives from
/// <c>config_schema.ServerBaseModel</c>, whose
/// <c>model_config = ConfigDict(extra="forbid")</c> refuses an unknown key at
/// <em>model construction</em> — so it is refused however the config was built,
/// not only when it came from a file. <c>config.config_from_mapping</c> catches
/// the resulting <c>ValidationError</c> and re-raises it as a <c>ValueError</c>
/// carrying the first error's message, which is pydantic's
/// <c>"Extra inputs are not permitted"</c>; the offending key is the error's
/// <c>loc</c>. Measured on the reference, not assumed:
/// <c>config_from_mapping({"brwoser_rate_limit_per_sec": 300})</c> raises
/// <c>ValueError("Extra inputs are not permitted")</c>.
///
/// This port's loader had no equivalent: it read the keys it knew and dropped
/// everything else in silence. So an operator who misspelled a security-relevant
/// key — <c>brwoser_rate_limit_per_sec</c>, <c>max_wokers</c> — got the default
/// and no warning, and a config that the reference server refuses to start with
/// booted here looking fine. The message therefore names the key: that is what
/// turns "your file is wrong" into "line 3 is wrong", and the reference's own
/// error carries the key too, just in a field its top-level formatter drops.
///
/// Two deliberate limits, both matching the reference rather than diverging from
/// it:
///
/// <em>The known set is the reference's field list, not this port's.</em> The C#
/// model does not carry <c>profiles</c>, <c>webhooks</c>, <c>pam</c> or
/// <c>audit</c> sections, but the reference accepts them, and one server.toml is
/// meant to be readable by any port. Refusing a section the canonical server
/// honours would be a worse divergence than ignoring it.
///
/// <em><c>[[sessions]]</c> is exempt, and must stay exempt.</em> Its
/// before-validator deliberately folds every unknown key into
/// <c>connector_config</c>, which is how a connector's open-ended options
/// (<c>host</c>, <c>port</c>, <c>username</c>) reach it at the top level of an
/// entry at all. That is the one documented hole in <c>extra="forbid"</c> and
/// this file pins it shut against a future sweep.
/// </summary>
public sealed class ServerConfigUnknownKeyTests
{
    private static string WriteToml(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-unknown-key-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, body);
        return path;
    }

    private static UtermServerConfig LoadToml(string body)
    {
        var path = WriteToml(body);
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    private static ArgumentException Refused(string body) =>
        Assert.Throws<ArgumentException>(() => LoadToml(body));

    // -- The refusal -------------------------------------------------------

    /// <summary>
    /// The case that motivated this: a near-miss on a security-relevant key.
    /// Before, the typo was dropped, the default applied, and the operator's
    /// only evidence was a rate limit that was not the one they wrote.
    /// </summary>
    [Fact]
    public void A_Typo_On_A_Security_Key_Is_Refused_By_Name()
    {
        var ex = Refused("brwoser_rate_limit_per_sec = 5\n");
        Assert.Contains("brwoser_rate_limit_per_sec", ex.Message, StringComparison.Ordinal);
        Assert.Contains("Extra inputs are not permitted", ex.Message, StringComparison.Ordinal);
    }

    /// <summary>
    /// An unrecognised scalar and an unrecognised table are the same mistake —
    /// a misspelled section heading is exactly as invisible as a misspelled key.
    /// </summary>
    [Theory]
    [InlineData("unknown_thing = 1")]
    [InlineData("max_workerz = 5")]
    [InlineData("[securityz]\nmode = \"strict\"")]
    [InlineData("[[graphical_target]]\ntarget_id = \"g1\"")]
    public void An_Unrecognised_Top_Level_Key_Is_Refused(string body)
    {
        Assert.Contains("Extra inputs are not permitted", Refused(body + "\n").Message, StringComparison.Ordinal);
    }

    /// <summary>
    /// Refusal is refusal, not partial application: a file with one bad key does
    /// not get its good keys installed on the way to the exception. The
    /// reference gets this free — nothing exists until the model validates — and
    /// this port has to check before it applies anything.
    /// </summary>
    [Fact]
    public void A_Refused_File_Applies_Nothing()
    {
        var path = WriteToml("""
            environment = "dev"
            typo_here = true
            """);
        try
        {
            var cfg = UtermServerConfig.Default();
            Assert.Throws<ArgumentException>(() => ConfigLoader.Load(path));
            // The loader builds its own config, so the observable claim is that a
            // fresh load never returns — nothing half-built escapes.
            Assert.Equal("production", cfg.Environment);
        }
        finally
        {
            File.Delete(path);
        }
    }

    // -- Everything the reference accepts still loads -----------------------

    /// <summary>
    /// Every top-level key of the reference's model, in one file. This is the
    /// regression that matters most: a known-key list is only safe if it is
    /// complete, and an incomplete one refuses working deployments.
    /// </summary>
    [Fact]
    public void The_Whole_Reference_Key_Surface_Still_Loads()
    {
        var cfg = LoadToml("""
            environment = "dev"
            session_idle_timeout_s = 60
            session_retention_s = 120
            browser_rate_limit_per_sec = 50
            rest_acquire_rate_limit_per_sec = 5
            rest_send_rate_limit_per_sec = 20
            worker_frame_on_invalid = "reject"
            max_connections_per_principal = 7
            max_workers = 99

            [server]
            host = "127.0.0.1"

            [auth]
            mode = "jwt"

            [control_plane]
            backend = "memory"

            [ui]
            app_path = "/app"

            [recording]
            enabled_by_default = true

            [profiles]
            directory = "profiles.d"

            [security]
            mode = "strict"

            [tunnel]
            token_ttl_s = 60

            [webhooks]
            allow_loopback_destinations = false

            [pam]
            mode = "off"

            [governance]
            authz_webhook_timeout_s = 1.0

            [audit]
            chain_enabled = false

            [[sessions]]
            session_id = "s1"

            [[graphical_targets]]
            target_id = "g1"
            """);

        Assert.Equal("dev", cfg.Environment);
        Assert.Equal(50, cfg.BrowserRateLimitPerSec);
        Assert.Equal(99, cfg.MaxWorkers);
        Assert.Equal("reject", cfg.WorkerFrameOnInvalid);
        Assert.Equal("s1", Assert.Single(cfg.Sessions).SessionId);
        Assert.Equal("g1", Assert.Single(cfg.GraphicalTargets).TargetId);
    }

    /// <summary>
    /// Sections the reference models and this port does not are accepted and
    /// ignored, not refused. One server.toml should be readable by any port; a
    /// C# server that will not start on a file the canonical server honours is a
    /// worse failure than one that quietly does not implement a section.
    /// </summary>
    [Fact]
    public void A_Section_This_Port_Does_Not_Model_Is_Accepted_And_Ignored()
    {
        var cfg = LoadToml("""
            [pam]
            mode = "off"
            auto_session = false

            [audit]
            chain_enabled = true
            """);

        Assert.Equal("production", cfg.Environment);
    }

    /// <summary>A file that says nothing unknown is unaffected.</summary>
    [Fact]
    public void A_Clean_File_Is_Unaffected()
    {
        var cfg = LoadToml("""
            [server]
            host = "127.0.0.1"
            port = 9111
            """);
        Assert.Equal(9111, cfg.Server.Port);
    }

    // -- The documented exception ------------------------------------------

    /// <summary>
    /// The <c>[[sessions]]</c> hole, held open on purpose. A connector's options
    /// are open-ended by design and reach it through the entry's unknown keys;
    /// the reference's before-validator sweeps them into
    /// <c>connector_config</c> before <c>extra="forbid"</c> ever looks. Sweeping
    /// unknown keys out of a sessions entry would silently break every SSH and
    /// telnet session definition in existence.
    /// </summary>
    [Fact]
    public void Unknown_Keys_In_A_Sessions_Entry_Still_Reach_Connector_Config()
    {
        var cfg = LoadToml("""
            [[sessions]]
            session_id = "ssh-box"
            connector_type = "ssh"
            host = "10.0.0.9"
            port = 2222
            username = "root"
            """);

        var session = Assert.Single(cfg.Sessions);
        Assert.Equal("ssh-box", session.SessionId);
        Assert.Equal("10.0.0.9", session.ConnectorConfig["host"]);
        Assert.Equal(2222L, session.ConnectorConfig["port"]);
        Assert.Equal("root", session.ConnectorConfig["username"]);
    }

    /// <summary>
    /// And the exemption is scoped to the entries, not to the whole file: a typo
    /// beside a <c>[[sessions]]</c> block is still a typo.
    /// </summary>
    [Fact]
    public void A_Sessions_Entry_Does_Not_Excuse_The_Rest_Of_The_File()
    {
        var ex = Refused("""
            sessionz = "typo"

            [[sessions]]
            session_id = "s1"
            """);
        Assert.Contains("sessionz", ex.Message, StringComparison.Ordinal);
    }
}
