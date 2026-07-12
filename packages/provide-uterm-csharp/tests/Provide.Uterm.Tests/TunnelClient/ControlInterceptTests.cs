//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.Gateway;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests.TunnelClient;

public class ControlInterceptTests
{
    [Fact]
    public void Control_Open_Resize_Frames_Decode()
    {
        var open = TunnelControl.OpenTerminalFrame(80, 24);
        var frame = TunnelCodec.DecodeFrame(open);
        Assert.True(frame.IsControl);
        var msg = TunnelCodec.DecodeControl(frame.Payload);
        Assert.Equal("open", msg["type"]?.ToString());
        Assert.Equal("terminal", msg["tunnel_type"]?.ToString());

        var resize = TunnelControl.ResizeFrame(120, 40);
        var rmsg = TunnelCodec.DecodeControl(TunnelCodec.DecodeFrame(resize).Payload);
        Assert.Equal("resize", rmsg["type"]?.ToString());
        Assert.Equal(120L, Convert.ToInt64(rmsg["cols"]));

        var tcp = TunnelControl.OpenTcpFrame(8080);
        var tmsg = TunnelCodec.DecodeControl(TunnelCodec.DecodeFrame(tcp).Payload);
        Assert.Equal("tcp", tmsg["tunnel_type"]?.ToString());

        var http = TunnelControl.OpenHttpFrame(9000);
        var hmsg = TunnelCodec.DecodeControl(TunnelCodec.DecodeFrame(http).Payload);
        Assert.Equal("http", hmsg["tunnel_type"]?.ToString());

        var compact = TunnelControl.MarshalCompact(new Dictionary<string, object?> { ["type"] = "x" });
        Assert.NotEmpty(compact);
    }

    [Fact]
    public void Intercept_SanitizeHeaders_DropsDenylist()
    {
        var (cleaned, dropped) = InterceptHeaders.SanitizeHeaders(new Dictionary<string, string>
        {
            ["X-Custom"] = "ok",
            ["Authorization"] = "secret",
            ["Host"] = "evil",
            ["Content-Length"] = "9",
            ["Accept"] = "text/plain",
        });
        Assert.Contains("X-Custom", cleaned.Keys);
        Assert.Contains("Accept", cleaned.Keys);
        Assert.DoesNotContain("Authorization", cleaned.Keys);
        Assert.True(dropped.Count >= 3);
    }

    [Fact]
    public void Intercept_ParseActionMessage_Modify_And_Fallback()
    {
        var drop = InterceptHeaders.ParseActionMessage(new Dictionary<string, object?>
        {
            ["action"] = "drop",
        });
        Assert.Equal("drop", drop.Action);

        var unk = InterceptHeaders.ParseActionMessage(new Dictionary<string, object?>
        {
            ["action"] = "explode",
        });
        Assert.Equal("forward", unk.Action);

        var body = Convert.ToBase64String(Encoding.UTF8.GetBytes("hello"));
        var mod = InterceptHeaders.ParseActionMessage(new Dictionary<string, object?>
        {
            ["action"] = "modify",
            ["headers"] = new Dictionary<string, object?>
            {
                ["X-A"] = "1",
                ["Authorization"] = "nope",
                ["n"] = 2.0,
            },
            ["body_b64"] = body,
        });
        Assert.Equal("modify", mod.Action);
        Assert.NotNull(mod.Headers);
        Assert.DoesNotContain("Authorization", mod.Headers!.Keys);
        Assert.Equal("hello", Encoding.UTF8.GetString(mod.Body!));

        var badB64 = InterceptHeaders.ParseActionMessage(new Dictionary<string, object?>
        {
            ["action"] = "modify",
            ["body_b64"] = "!!!not-base64!!!",
        });
        Assert.Null(badB64.Body);

        // string header map path
        var strHeaders = InterceptHeaders.ParseActionMessage(new Dictionary<string, object?>
        {
            ["action"] = "modify",
            ["headers"] = new Dictionary<string, string> { ["X-Ok"] = "v", ["Host"] = "no" },
        });
        Assert.Contains("X-Ok", strHeaders.Headers!.Keys);

        Assert.Equal("True", InterceptHeaders.StringifyHeaderValue(true));
        Assert.Equal("False", InterceptHeaders.StringifyHeaderValue(false));
        Assert.Equal("3", InterceptHeaders.StringifyHeaderValue(3.0));
        Assert.Equal("", InterceptHeaders.StringifyHeaderValue(null));
    }

    [Fact]
    public async Task InterceptGate_Await_Resolve_And_Timeout()
    {
        var clamped = new InterceptGate(timeoutS: 0.05, timeoutAction: "nope");
        Assert.Equal(1.0, clamped.TimeoutS); // clamped
        Assert.Equal("forward", clamped.TimeoutAction); // invalid action coerced

        var gate = new InterceptGate(timeoutS: 1.0, timeoutAction: "drop");
        Assert.Equal("drop", gate.TimeoutAction);
        gate.Enabled = true;
        Assert.True(gate.Enabled);
        gate.InspectEnabled = false;
        Assert.False(gate.InspectEnabled);

        gate.RegisterPending("r1");
        var wait = gate.AwaitDecisionAsync("r1");
        Assert.True(gate.Resolve("r1", new InterceptDecision { Action = "modify" }));
        var d = await wait;
        Assert.Equal("modify", d.Action);

        // await missing id → immediate timeout-action (no pending TCS)
        var miss = await gate.AwaitDecisionAsync("ghost");
        Assert.Equal("drop", miss.Action);

        // resolve missing
        Assert.False(gate.Resolve("nope", new InterceptDecision()));
    }

    [Fact]
    public void Gateway_Pump_ControlFrames_And_BindPolicy()
    {
        var hello = GatewayPump.HelloFrame();
        Assert.Equal("hello", hello["type"]?.ToString());

        var st = new GatewayPump.ControlState();
        Assert.True(GatewayPump.HandleControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "session_token",
            ["token"] = "t1",
            ["player_id"] = 42,
        }, st));
        Assert.Equal("t1", st.Token!.Token);
        Assert.Equal(42, st.Token.PlayerId);

        // AsInt64 variants + missing token + redirect without path
        Assert.True(GatewayPump.HandleControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "session_token",
            ["token"] = "t2",
            ["player_id"] = "99",
        }, st));
        Assert.Equal(99, st.Token!.PlayerId);
        Assert.False(GatewayPump.HandleControlFrame(
            new Dictionary<string, object?> { ["type"] = "session_token" }, st));
        Assert.False(GatewayPump.HandleControlFrame(
            new Dictionary<string, object?> { ["type"] = "redirect" }, st));
        Assert.False(GatewayPump.HandleControlFrame(
            new Dictionary<string, object?> { ["no_type"] = 1 }, st));

        var resumeNoPid = GatewayPump.ResumeFrame(new GatewayPump.TokenRec { Token = "z" });
        Assert.False(resumeNoPid.ContainsKey("player_id"));

        var wrote = new List<string>();
        Assert.True(GatewayPump.HandleControlFrame(
            new Dictionary<string, object?> { ["type"] = "resume_ok" },
            st,
            b => wrote.Add(Encoding.UTF8.GetString(b))));
        Assert.Contains(wrote, s => s.Contains("resumed", StringComparison.OrdinalIgnoreCase));

        Assert.True(GatewayPump.HandleControlFrame(
            new Dictionary<string, object?> { ["type"] = "resume_failed" }, st));
        Assert.Null(st.Token);

        Assert.True(GatewayPump.HandleControlFrame(
            new Dictionary<string, object?> { ["type"] = "redirect", ["path"] = "/ws/x" }, st));
        Assert.Equal("/ws/x", st.Redirect);

        Assert.False(GatewayPump.HandleControlFrame(
            new Dictionary<string, object?> { ["type"] = "noise" }, st));

        var resume = GatewayPump.ResumeFrame(new GatewayPump.TokenRec { Token = "abc", PlayerId = 7 });
        Assert.Equal("resume", resume["type"]?.ToString());
        Assert.Equal(7L, Convert.ToInt64(resume["player_id"]));

        Assert.True(GatewayBindPolicy.IsLoopbackBindHost("127.0.0.1"));
        Assert.True(GatewayBindPolicy.IsLoopbackBindHost("localhost"));
        Assert.False(GatewayBindPolicy.IsLoopbackBindHost("0.0.0.0"));
        Assert.False(GatewayBindPolicy.IsLoopbackBindHost("1.2.3.4"));

        Assert.Throws<InvalidOperationException>(() =>
            GatewayBindPolicy.RequireUnauthenticatedAllowed("0.0.0.0", allowUnauthenticated: false));
        GatewayBindPolicy.RequireUnauthenticatedAllowed("0.0.0.0", allowUnauthenticated: true);
        GatewayBindPolicy.RequireUnauthenticatedAllowed("127.0.0.1", allowUnauthenticated: false);
    }

    [Fact]
    public async Task TelnetGateway_Rejects_NonLoopback_Without_Allow()
    {
        await using var gw = new TelnetGateway();
        await Assert.ThrowsAsync<InvalidOperationException>(() => gw.StartAsync("0.0.0.0", 0));
        gw.AllowUnauthenticated = true;
        // may fail if port 2112 busy — use FreePort via StartAsync with explicit port after allow
        // port 0 maps to fixed default; pick free port
        var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        var p = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        await gw.StartAsync("127.0.0.1", p);
        await gw.StopAsync();
    }
}
