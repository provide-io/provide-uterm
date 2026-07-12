//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Connectors;
using Provide.Uterm.Shell;

namespace Provide.Uterm.Tests.Shell;

/// <summary>Drives real <see cref="UshellConnector"/> public methods (Go connector parity).</summary>
public class UshellConnectorTests
{
    private static UshellConnector NewConn(string id = "s1", Dictionary<string, object?>? extra = null) =>
        new(id, new UshellConnectorConfig
        {
            ExtraCtx = extra,
            PollSleep = _ => { }, // no idle sleep in tests
        });

    private static string FrameData(IReadOnlyList<Dictionary<string, object?>> frames) =>
        string.Join(" ", frames.Select(f => f.TryGetValue("data", out var d) ? d?.ToString() ?? "" : ""));

    [Fact]
    public void Lifecycle_Start_Stop_IsConnected()
    {
        var c = NewConn();
        Assert.False(c.IsConnected());
        c.Start();
        Assert.True(c.IsConnected());
        c.Stop();
        Assert.False(c.IsConnected());
    }

    [Fact]
    public void Poll_Welcome_Then_FlowPause_Withholds_Pending()
    {
        var c = NewConn();
        Assert.Empty(c.PollMessages()); // not connected
        c.Start();
        var welcome = c.PollMessages();
        Assert.Equal(2, welcome.Count);
        Assert.Equal("worker_hello", welcome[0]["type"]);
        Assert.Equal("open", welcome[0]["input_mode"]);
        Assert.Equal("term", welcome[1]["type"]);
        Assert.Contains("ushell", welcome[1]["data"]?.ToString() ?? "", StringComparison.Ordinal);

        // inject pending via HandleInput then pause
        _ = c.HandleInput("help\r");
        Assert.Empty(c.HandleControl("flow_pause"));
        Assert.Empty(c.PollMessages()); // withheld
        Assert.Empty(c.HandleControl("flow_resume"));
        // help output was returned from HandleInput, not pending — seed pending via IConnector path
        var ic = (IConnector)c;
        ic.HandleInputAsync("clear\r").GetAwaiter().GetResult();
        var polled = c.PollMessages();
        Assert.NotEmpty(polled);
        Assert.Contains("2J", FrameData(polled), StringComparison.Ordinal);
    }

    [Fact]
    public void HandleInput_Echo_And_Dispatch_Help()
    {
        var c = NewConn();
        c.Start();
        var echo = c.HandleInput("ab");
        Assert.Single(echo);
        Assert.Equal("ab", echo[0]["data"]);

        var dispatched = c.HandleInput("c\r");
        // echo "c\r\n" + help text frames
        Assert.True(dispatched.Count >= 1);
        Assert.Contains("c", FrameData(dispatched), StringComparison.Ordinal);
    }

    [Fact]
    public void HandleControl_Snapshot_And_HijackNoops()
    {
        var c = NewConn("snap");
        c.Start();
        _ = c.HandleInput("xy");
        var snap = c.HandleControl("snapshot_request");
        Assert.Single(snap);
        Assert.Equal("snapshot", snap[0]["type"]);
        Assert.Contains("ushell snap", snap[0]["screen"]?.ToString() ?? "", StringComparison.Ordinal);
        Assert.Contains("xy", snap[0]["screen"]?.ToString() ?? "", StringComparison.Ordinal);

        foreach (var a in new[] { "pause", "resume", "step" })
        {
            Assert.Empty(c.HandleControl(a));
        }
    }

    [Fact]
    public void GetAnalysis_Clear_SetMode()
    {
        var c = NewConn("ana", new Dictionary<string, object?> { ["alpha"] = 1, ["__hid"] = 2 });
        c.Start();
        _ = c.HandleInput("z");
        var analysis = c.GetAnalysis();
        Assert.Contains("session: ana", analysis, StringComparison.Ordinal);
        Assert.Contains("connected: true", analysis, StringComparison.Ordinal);
        Assert.Contains("current_line: \"z\"", analysis, StringComparison.Ordinal);
        Assert.Contains("alpha", analysis, StringComparison.Ordinal);
        Assert.DoesNotContain("__hid", analysis, StringComparison.Ordinal);

        var cleared = c.ClearScreen();
        Assert.Contains("2J", FrameData(cleared), StringComparison.Ordinal);
        Assert.DoesNotContain("z", c.GetSnapshot()["screen"]?.ToString() ?? "z", StringComparison.Ordinal);

        var mode = c.SetMode("hijack");
        Assert.Equal("worker_hello", mode[0]["type"]);
        Assert.Equal("hijack", mode[0]["input_mode"]);
    }

    [Fact]
    public async Task IConnector_Registry_Ushell_And_Events()
    {
        var reg = new ConnectorRegistry();
        Assert.Contains("ushell", reg.Types());
        var conn = reg.Create("ushell", new Dictionary<string, object?>
        {
            ["session_id"] = "reg1",
            ["display_name"] = "Pretty",
        });
        Assert.IsType<UshellConnector>(conn);
        var ush = (UshellConnector)conn;
        Assert.Equal("Pretty", ush.DisplayName);
        await conn.StartAsync();
        Assert.True(conn.IsConnected());
        var welcome = conn.Events();
        Assert.Equal(2, welcome.Count);
        Assert.Equal("worker_hello", welcome[0]["type"]);
        await conn.HandleInputAsync("help\r");
        var more = conn.Events();
        Assert.NotEmpty(more);
        Assert.Contains("ushell commands", FrameData(more), StringComparison.OrdinalIgnoreCase);
        conn.HandleControl("flow_pause");
        Assert.Empty(conn.Events());
        conn.HandleControl("flow_resume");
        var snap = conn.Snapshot();
        Assert.Contains("ushell", snap.Screen, StringComparison.Ordinal);
        Assert.Contains("analysis", conn.Analysis(), StringComparison.Ordinal);
        conn.Clear();
        conn.SetMode("open");
        Assert.Null(conn.Session());
        await conn.StopAsync();
        Assert.False(conn.IsConnected());
    }

    [Fact]
    public void Animated_Render_Queues_Frames_On_Poll()
    {
        var c = new UshellConnector("anim", new UshellConnectorConfig
        {
            PollSleep = _ => { },
            RenderImage = (_, _, _, _) => (new[] { "F1", "F2" }, 100.0),
        });
        c.Start();
        _ = c.PollMessages(); // welcome
        // write temp image file for render
        var path = Path.Combine(Path.GetTempPath(), "ush-anim-" + Guid.NewGuid().ToString("N"));
        File.WriteAllBytes(path, new byte[] { 1, 2, 3 });
        try
        {
            _ = c.HandleInput("render --loop file://" + path + "\r");
            // animation streams into pending
            var deadline = DateTime.UtcNow.AddSeconds(2);
            var saw = new List<string>();
            while (DateTime.UtcNow < deadline && saw.Count < 2)
            {
                foreach (var f in c.PollMessages())
                {
                    if (f.TryGetValue("data", out var d) && d is string s)
                    {
                        saw.Add(s);
                    }
                }

                Thread.Sleep(5);
            }

            Assert.Contains(saw, s => s.Contains("F1", StringComparison.Ordinal) || s.Contains("F2", StringComparison.Ordinal));
        }
        finally
        {
            File.Delete(path);
            c.Stop();
        }
    }
}
