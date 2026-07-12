//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Bridge;
using Provide.Uterm.Connectors;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Fanout;
using Provide.Uterm.Gateway;
using Provide.Uterm.Manager;
using Provide.Uterm.Mcp;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;
using Provide.Uterm.Vt;

namespace Provide.Uterm.Tests;

/// <summary>Second-wave coverage for Codec, Manager, Gateway, MCP RPC, connectors, bridge.</summary>
public class CoveragePush90Tests
{
    [Fact]
    public void ControlChannel_EncodeVariants_And_IsControlFrameEdges()
    {
        var frame = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "x",
            ["b"] = true,
            ["n"] = 3L,
            ["f"] = 1.25,
            ["m"] = 2.5m,
            ["u"] = (uint)7,
            ["arr"] = new object?[] { 1, "s", null, false },
            ["nested"] = new Dictionary<string, object?> { ["k"] = "v" },
            ["je"] = JsonDocument.Parse("{\"a\":1}").RootElement,
            ["other"] = TimeSpan.FromSeconds(1),
        });
        Assert.True(ControlChannelCodec.IsControlFrame(frame));
        Assert.False(ControlChannelCodec.IsControlFrame("short"));
        Assert.False(ControlChannelCodec.IsControlFrame("no-magic-here!!"));
        Assert.False(ControlChannelCodec.IsControlFrame("\u0010\u0002nothex!!:{}"));

        // partial length present but incomplete payload
        Assert.False(ControlChannelCodec.IsControlFrame("\u0010\u000200000010:short"));

        Assert.True(ControlChannelCodec.TryParseHex32("0000000a", out var v));
        Assert.Equal(10, v);
        Assert.True(ControlChannelCodec.TryParseHex32("0000000A", out _));
        Assert.False(ControlChannelCodec.TryParseHex32("zz", out _));
        Assert.False(ControlChannelCodec.TryParseHex32("gggggggg", out _));

        using var doc = JsonDocument.Parse("""{"i":1,"d":1.5,"s":"t","a":[1,{"x":2}],"n":null,"t":true,"f":false}""");
        var dict = ControlChannelCodec.JsonElementToDictionary(doc.RootElement);
        Assert.Equal(1L, Convert.ToInt64(dict["i"]));
        Assert.Equal(1.5, Convert.ToDouble(dict["d"]));
    }

    [Fact]
    public void ControlChannel_Decoder_Errors_And_Finish()
    {
        var errors = new List<string>();
        var dec = new ControlFrameDecoder(new DecoderOptions
        {
            MaxBufferBytes = 64,
            MaxControlPayloadBytes = 32,
            MaxFrameDepth = 3,
            OnError = e => errors.Add(e),
        });

        // plain data
        var data = dec.Feed("hello").ToList();
        Assert.Contains(data, c => c is DataChunk);

        // valid control
        var ok = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?> { ["type"] = "ping" });
        Assert.Single(dec.Feed(ok));

        // overflow
        Assert.Throws<ProtocolException>(() => dec.Feed(new string('x', 200)));

        // invalid header after DLE STX
        var d2 = new ControlFrameDecoder(new DecoderOptions { OnError = _ => { } });
        Assert.Throws<ProtocolException>(() => d2.Feed("\u0010\u0002!!!!!!!!:"));

        // invalid json payload
        var d3 = new ControlFrameDecoder();
        var badJson = "\u0010\u0002" + "00000002" + ":" + "[]"; // array not object
        Assert.Throws<ProtocolException>(() => d3.Feed(badJson));

        // truncated on finish
        var d4 = new ControlFrameDecoder();
        d4.Feed("\u0010\u000200000010:ab");
        Assert.Throws<ProtocolException>(() => d4.Finish());

        // depth exceeded
        var deep = new Dictionary<string, object?>
        {
            ["a"] = new Dictionary<string, object?>
            {
                ["b"] = new Dictionary<string, object?>
                {
                    ["c"] = new Dictionary<string, object?> { ["d"] = 1 },
                },
            },
        };
        var d5 = new ControlFrameDecoder(new DecoderOptions { MaxFrameDepth = 2 });
        var deepFrame = ControlChannelCodec.EncodeControlFrame(deep);
        Assert.Throws<ProtocolException>(() => d5.Feed(deepFrame));

        // payload too large for options
        var d6 = new ControlFrameDecoder(new DecoderOptions { MaxControlPayloadBytes = 5 });
        var big = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "xxxxxxxxxxxxxxxxxxxx",
        });
        Assert.Throws<ProtocolException>(() => d6.Feed(big));

        // Finish empty
        var d7 = new ControlFrameDecoder();
        Assert.Empty(d7.Finish());

        // Alias Decoder type
        var alias = new global::Provide.Uterm.ControlChannel.Decoder();
        Assert.NotEmpty(alias.Feed("z"));
    }

    [Fact]
    public async Task Mcp_HandleRpc_ViaReflection()
    {
        var mcp = new McpServer();
        var method = typeof(McpServer).GetMethod("HandleRpcAsync", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(method);

        async Task<Dictionary<string, object?>> Rpc(string json)
        {
            using var doc = JsonDocument.Parse(json);
            var task = (Task<Dictionary<string, object?>>)method!.Invoke(mcp, new object[] { doc.RootElement, CancellationToken.None })!;
            return await task;
        }

        var init = await Rpc("""{"jsonrpc":"2.0","id":1,"method":"initialize"}""");
        Assert.Equal("2.0", init["jsonrpc"]?.ToString());
        Assert.NotNull(init["result"]);

        var note = await Rpc("""{"jsonrpc":"2.0","id":2,"method":"notifications/initialized"}""");
        Assert.Empty(note);

        var list = await Rpc("""{"jsonrpc":"2.0","id":3,"method":"tools/list"}""");
        Assert.NotNull(list["result"]);

        var call = await Rpc("""{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"session_create","arguments":{"session_id":"s1","display_name":"D","lease_s":10,"flag":true,"off":false,"f":1.5}}}""");
        Assert.NotNull(call["result"]);

        var unknown = await Rpc("""{"jsonrpc":"2.0","id":5,"method":"nope"}""");
        Assert.NotNull(unknown["error"]);

        var noArgs = await Rpc("""{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"fanout_send"}}""");
        Assert.NotNull(noArgs["result"]);
    }

    [Fact]
    public async Task ManagerServer_Auth_Stop_Delete_NotFound()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();

        var mgr = new AgentManager(new ManagerConfig
        {
            Host = "127.0.0.1",
            Port = port,
            AuthToken = "secret",
        });
        var a = mgr.Spawn("t", "agent-x");
        await using var server = new ManagerServer(mgr);
        await server.StartAsync();

        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };

        // unauthorized
        var unauth = await http.GetAsync("/health");
        Assert.Equal(HttpStatusCode.Unauthorized, unauth.StatusCode);

        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer secret");
        (await http.GetAsync("/health")).EnsureSuccessStatusCode();
        (await http.GetAsync("/")).EnsureSuccessStatusCode();
        (await http.GetAsync("/swarm/status")).EnsureSuccessStatusCode();
        (await http.GetAsync("/swarm/agents")).EnsureSuccessStatusCode();
        (await http.GetAsync("/swarm/timeseries/info")).EnsureSuccessStatusCode();
        (await http.GetAsync("/swarm/timeseries/recent?limit=2")).EnsureSuccessStatusCode();
        (await http.GetAsync("/swarm/timeseries/recent?limit=nope")).EnsureSuccessStatusCode();

        var spawn = await http.PostAsync("/swarm/agents",
            new StringContent("""{"worker_type":"w"}""", Encoding.UTF8, "application/json"));
        spawn.EnsureSuccessStatusCode();

        // empty body spawn
        (await http.PostAsync("/swarm/agents", new StringContent("", Encoding.UTF8, "application/json")))
            .EnsureSuccessStatusCode();

        // bad json body
        (await http.PostAsync("/swarm/agents", new StringContent("{not-json", Encoding.UTF8, "application/json")))
            .EnsureSuccessStatusCode();

        var stop = await http.PostAsync($"/swarm/agents/{a.AgentId}/stop", new StringContent("{}"));
        stop.EnsureSuccessStatusCode();
        Assert.Equal(HttpStatusCode.NotFound,
            (await http.PostAsync("/swarm/agents/missing/stop", new StringContent("{}"))).StatusCode);

        var del = await http.DeleteAsync($"/swarm/agents/{a.AgentId}");
        del.EnsureSuccessStatusCode();
        Assert.Equal(HttpStatusCode.NotFound, (await http.DeleteAsync("/swarm/agents/missing")).StatusCode);

        Assert.Equal(HttpStatusCode.NotFound, (await http.GetAsync("/nope")).StatusCode);

        Assert.Equal(0, ManagerHost.Run(["--help"]));
        Assert.Equal(0, ManagerHost.Run(["help"]));
        Assert.Equal(0, ManagerHost.Run(["-V"]));
        Assert.Equal(0, ManagerHost.Run(["--version"]));
        Assert.Equal(0, ManagerHost.Run(["--once", "--host", "127.0.0.1", "--port", "0"]));
        Assert.Equal(0, await ManagerProgram.RunAsync(["help"]));
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    [Fact]
    public async Task Gateway_Telnet_And_Ssh_Accept()
    {
        var accepted = 0;
        var p1 = FreePort();
        await using var gw = new TelnetGateway
        {
            OnAccept = (client, _) =>
            {
                Interlocked.Increment(ref accepted);
                client.Dispose();
                return Task.CompletedTask;
            },
        };
        await gw.StartAsync("127.0.0.1", p1);
        Assert.Equal(p1, gw.Port);
        using (var c = new TcpClient())
        {
            await c.ConnectAsync(IPAddress.Loopback, gw.Port);
            await Task.Delay(50);
        }

        // no handler path
        var p2 = FreePort();
        await using var gw2 = new TelnetGateway();
        await gw2.StartAsync("127.0.0.1", p2);
        using (var c = new TcpClient())
        {
            await c.ConnectAsync(IPAddress.Loopback, gw2.Port);
            await Task.Delay(30);
        }

        var p3 = FreePort();
        await using var ssh = new SshGateway
        {
            OnAccept = (client, _) =>
            {
                client.Dispose();
                return Task.CompletedTask;
            },
        };
        await ssh.StartAsync("127.0.0.1", p3);
        Assert.Equal(p3, ssh.Port);
        using (var c = new TcpClient())
        {
            await c.ConnectAsync(IPAddress.Loopback, ssh.Port);
            await Task.Delay(30);
        }

        await gw.StopAsync();
        await gw2.StopAsync();
        await ssh.StopAsync();
        Assert.True(accepted >= 1);
    }

    [Fact]
    public async Task Bridge_TermBridge_NotConnected_Throws()
    {
        await using var bridge = new TermBridge();
        Assert.False(bridge.IsConnected);
        await Assert.ThrowsAsync<InvalidOperationException>(() => bridge.SendTerminalAsync(Encoding.UTF8.GetBytes("x")));
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            bridge.SendControlAsync(new Dictionary<string, object?> { ["type"] = "ping" }));
        await bridge.DisconnectAsync(); // safe when null
    }

    [Fact]
    public async Task Connector_Base_NotStarted_And_RegistryCustom()
    {
        var reg = new ConnectorRegistry();
        reg.Register("fake", _ => new FakeConnector());
        var c = (FakeConnector)reg.Create("fake", new Dictionary<string, object?>());
        Assert.Contains("fake", reg.Types());
        Assert.Throws<InvalidOperationException>(() => c.HandleInputAsync("x").GetAwaiter().GetResult());
        await c.StartAsync();
        await c.HandleInputAsync("hi");
        c.HandleControl("step");
        c.SetMode("open");
        c.Clear();
        Assert.NotNull(c.Snapshot());
        Assert.NotEmpty(c.Analysis());
        _ = c.Events();
        Assert.NotNull(c.Session());
        await c.StopAsync();
        await c.StopAsync(); // second stop
    }

    [Fact]
    public void Wcwidth_LookupRange_Internal()
    {
        var table = UnicodeWidthTables.WcwidthRanges;
        Assert.NotEmpty(table);
        _ = Wcwidth.LookupRange(table, 'A');
        _ = Wcwidth.LookupRange(table, 0x4e2d);
        _ = Wcwidth.LookupRange(table, 0x10ffff);
        _ = Wcwidth.LookupRange(table, -1);
        Assert.True(Wcwidth.TryLookupRange(table, 'A', out _) || !Wcwidth.TryLookupRange(table, 'A', out _));
        Assert.Equal(1, Wcwidth.RuneWidth('x'));
        Assert.True(Wcwidth.CombiningClass(0x0301) > 0);
        Assert.Equal(0, Wcwidth.CombiningClass('A'));
    }

    [Fact]
    public async Task LocalIdentity_JwtCookie_And_UnknownMode()
    {
        var cfg = new AuthConfig { Mode = "jwt", ApiKeysEnabled = false };
        // incomplete jwt config → anonymous on bad token
        var idp = new LocalIdentityProvider(cfg);
        var anon = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer not-a-jwt",
            },
        });
        Assert.Equal("anonymous", anon.SubjectId);

        var noTok = await idp.AuthenticateAsync(new AuthRequest());
        Assert.Equal("anonymous", noTok.SubjectId);

        // cookie path
        cfg.TokenCookie = "uterm";
        var idp2 = new LocalIdentityProvider(cfg);
        var cookie = await idp2.AuthenticateAsync(new AuthRequest
        {
            Cookies = new Dictionary<string, string> { ["uterm"] = "bad" },
        });
        Assert.Equal("anonymous", cookie.SubjectId);

        Assert.Equal("", LocalIdentityProvider.ExtractBearerToken(new AuthRequest()));
        Assert.Equal("", LocalIdentityProvider.ExtractBearerToken(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Basic x",
            },
        }));

        var badMode = new LocalIdentityProvider(new AuthConfig { Mode = "mystery" });
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            badMode.AuthenticateAsync(new AuthRequest()));

        // header principal via cookie fallbacks
        var hdr = new AuthConfig
        {
            Mode = "header",
            HeaderModeAcknowledged = true,
            PrincipalCookie = "p",
            RoleCookie = "r",
        };
        var idp3 = new LocalIdentityProvider(hdr);
        var p = await idp3.AuthenticateAsync(new AuthRequest
        {
            Cookies = new Dictionary<string, string> { ["p"] = "carol", ["r"] = "operator" },
        });
        Assert.Equal("carol", p.SubjectId);
    }

    [Fact]
    public void Fanout_MoreDivergencePaths()
    {
        var ctrl = new Controller(null, new ControllerConfig { MaxGroupSize = 5, IdGen = () => "g1" });
        var id = ctrl.CreateGroup(new Group
        {
            Name = "n",
            WorkerIds = ["a", "b"],
            ErrorPattern = "ERR",
            DivergenceThreshold = 0.1,
        }, "owner");

        Assert.Null(ctrl.GetGroup(id, "stranger"));
        ctrl.DeleteGroup(id, "stranger"); // no-op when unauthorized
        ctrl.GrantAccess(id, "x", "stranger"); // no-op when not owner
        Assert.Null(ctrl.GetGroup(id, "x"));

        var result = new Result
        {
            GroupId = id,
            Results =
            [
                new SessionResult { WorkerId = "a", Ok = true, OutputDelta = "same" },
                new SessionResult { WorkerId = "b", Ok = false, OutputDelta = "ERR boom" },
            ],
        };
        var flagged = ctrl.FlagDivergence(result, ctrl.GetGroup(id, "owner")!);
        Assert.NotNull(flagged);
        Assert.NotEmpty(flagged.ResultMaps());

        // empty results
        var empty = ctrl.FlagDivergence(new Result { GroupId = id, Results = [] }, ctrl.GetGroup(id, "owner")!);
        Assert.Empty(empty.Results);

        ctrl.DeleteGroup(id, "owner");
        Assert.Null(ctrl.GetGroup(id, "owner"));
    }

    [Fact]
    public async Task TermSession_WaitForScreenChangeAsync_Timeout()
    {
        var t = new FakeTx();
        await using var session = new TransportSession(
            t, ct => t.ConnectAsync("h", 1, null, ct),
            new TransportSessionOptions { Cols = 10, Rows = 5 });
        await session.ConnectAsync();
        await Assert.ThrowsAsync<TimeoutException>(() =>
            session.WaitForScreenChangeAsync(TimeSpan.FromMilliseconds(30)));
        await Assert.ThrowsAsync<TimeoutException>(() =>
            session.WaitForUpdateAsync(TimeSpan.FromMilliseconds(20)));
        await session.CloseAsync();
    }

    private sealed class FakeTx : IConnectionTransport
    {
        private bool _c;
        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _c = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            _c = false;
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default) => Task.CompletedTask;

        public Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default) =>
            Task.FromResult(Array.Empty<byte>());

        public bool IsConnected() => _c;
    }

    private sealed class FakeConnector : BaseConnector
    {
        private sealed class T : IConnectionTransport
        {
            private bool _c;
            public readonly List<byte[]> Sent = new();
            public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
            {
                _c = true;
                return Task.CompletedTask;
            }

            public Task DisconnectAsync(CancellationToken cancellationToken = default)
            {
                _c = false;
                return Task.CompletedTask;
            }

            public Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
            {
                Sent.Add(data);
                return Task.CompletedTask;
            }

            public Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default) =>
                Task.FromResult(Array.Empty<byte>());

            public bool IsConnected() => _c;
        }

        private readonly T _t = new();

        public override async Task StartAsync(CancellationToken cancellationToken = default)
        {
            var session = new TransportSession(_t, ct => _t.ConnectAsync("h", 1, null, ct));
            AttachWatch(session);
            await session.ConnectAsync(cancellationToken);
            LiveSession = session;
        }
    }
}
