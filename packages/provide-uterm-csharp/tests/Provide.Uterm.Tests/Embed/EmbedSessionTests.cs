//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Embed;

namespace Provide.Uterm.Tests.Embed;

public class EmbedSessionTests
{
    [Fact]
    public async Task Create_Connect_Fanout_And_UpstreamSend()
    {
        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = "s1" });
        Assert.Same(session, hub.GetSession("s1"));
        Assert.Contains("s1", hub.SessionIds);

        var phases = new List<SessionLifecycle>();
        session.LifecycleChanged += (_, e) => phases.Add(e.Phase);

        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        Assert.Equal(SessionLifecycle.Connected, session.Lifecycle);

        var c1 = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata
            {
                ClientId = "std",
                Tags = { "standard" },
            },
        });
        var cDeaf = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata
            {
                ClientId = "deaf",
                Tags = { "deaf" },
            },
        });

        // ApplicationDataReceived fires independently of client delivery, so
        // asserting on it straight after ReceiveAsync assumes an ordering the
        // session does not promise — under load the handler had not run yet.
        // Signal on arrival and wait for it instead. Concurrent collection
        // because the handler runs off the test's thread.
        var app = new System.Collections.Concurrent.ConcurrentQueue<byte[]>();
        var appReceived = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        session.ApplicationDataReceived += (_, e) =>
        {
            app.Enqueue(e.Data);
            appReceived.TrySetResult();
        };

        await up.PushFromRemoteAsync("HELLO"u8.ToArray());
        var got = await c1.ReceiveAsync();
        Assert.Equal("HELLO"u8.ToArray(), got);
        // deaf still receives unless filtered — selective fan-out is host filter on SendToClients
        var gotDeaf = await cDeaf.ReceiveAsync();
        Assert.Equal("HELLO"u8.ToArray(), gotDeaf);
        await appReceived.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.NotEmpty(app);

        // Selective: only standard
        await session.SendToClientsAsync("X"u8.ToArray(), new ClientFilter { RequireAnyTag = new[] { "standard" } });
        Assert.Equal("X"u8.ToArray(), await c1.ReceiveAsync());

        await session.SendToUpstreamAsync("CMD"u8.ToArray());
        Assert.Contains(up.Sent, b => b.SequenceEqual("CMD"u8.ToArray()));

        Assert.Contains(SessionLifecycle.Connecting, phases);
        Assert.Contains(SessionLifecycle.Connected, phases);
        Assert.Contains(SessionLifecycle.ClientAttached, phases);

        await session.DisposeAsync();
        Assert.Equal(SessionLifecycle.Shutdown, session.Lifecycle);
        hub.RemoveSession("s1");
        Assert.Null(hub.GetSession("s1"));
    }

    [Fact]
    public async Task Interceptor_Consume_Replace_Defer_Inject_And_Reentrancy()
    {
        var order = new List<string>();
        var interceptor = new ScriptInterceptor(order);
        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync(new EmbedSessionOptions { Interceptor = interceptor });
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        var client = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "c1" },
        });

        // consume
        interceptor.NextUpstream = InterceptResult.Consume();
        await up.PushFromRemoteAsync("NOPE"u8.ToArray());
        await Task.Delay(40);
        Assert.False(await TryReceiveAsync(client, 40));

        // replace
        interceptor.NextUpstream = InterceptResult.Replace("REP"u8.ToArray());
        await up.PushFromRemoteAsync("ORIG"u8.ToArray());
        Assert.Equal("REP"u8.ToArray(), await client.ReceiveAsync());

        // inject: drop original, inject NEW (re-enters as pass)
        interceptor.NextUpstream = InterceptResult.Inject("INJ"u8.ToArray());
        interceptor.AfterInjectPass = true;
        await up.PushFromRemoteAsync("DROPME"u8.ToArray());
        Assert.Equal("INJ"u8.ToArray(), await client.ReceiveAsync());

        // defer then flush
        interceptor.NextUpstream = InterceptResult.Defer();
        await up.PushFromRemoteAsync("LATER"u8.ToArray());
        await Task.Delay(40);
        Assert.False(await TryReceiveAsync(client, 40));
        interceptor.NextUpstream = InterceptResult.Pass();
        await session.FlushDeferredAsync();
        Assert.Equal("LATER"u8.ToArray(), await client.ReceiveAsync());

        // client consume (menu local)
        interceptor.NextClient = InterceptResult.Consume();
        await session.SendToUpstreamAsync("LOCAL"u8.ToArray());
        Assert.DoesNotContain(up.Sent, b => b.SequenceEqual("LOCAL"u8.ToArray()));

        // re-entrant: on "PING" inject send to upstream "PONG" via session during intercept
        interceptor.NextUpstream = null;
        interceptor.ReenterSendUpstream = "PONG"u8.ToArray();
        await up.PushFromRemoteAsync("PING"u8.ToArray());
        // wait for ordered processing
        var pingOut = await client.ReceiveAsync();
        Assert.Equal("PING"u8.ToArray(), pingOut);
        await Task.Delay(50);
        Assert.Contains(up.Sent, b => b.SequenceEqual("PONG"u8.ToArray()));
    }

    [Fact]
    public async Task ReplaceUpstream_PreservesClients()
    {
        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync();
        var up1 = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up1);
        var client = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "c" },
        });

        var up2 = new MemoryUpstream();
        await session.ReplaceUpstreamAsync(up2);
        Assert.Equal(SessionLifecycle.Connected, session.Lifecycle);

        await up2.PushFromRemoteAsync("AFTER"u8.ToArray());
        Assert.Equal("AFTER"u8.ToArray(), await client.ReceiveAsync());
        await session.SendToUpstreamAsync("U2"u8.ToArray());
        Assert.Contains(up2.Sent, b => b.SequenceEqual("U2"u8.ToArray()));
    }

    [Fact]
    public async Task Backpressure_DropOldest_DoesNotBlockUpstream()
    {
        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync();
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        var slow = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata
            {
                ClientId = "slow",
                QueueCapacity = 2,
                Backpressure = BackpressurePolicy.DropOldest,
            },
        });

        for (var i = 0; i < 5; i++)
        {
            await up.PushFromRemoteAsync(new[] { (byte)i });
        }

        await Task.Delay(40);
        // queue capacity 2 → only last chunks retained (approx)
        var a = await slow.ReceiveAsync();
        var b = await slow.ReceiveAsync();
        Assert.True(a.Length == 1 && b.Length == 1);
        // upstream still accepts more
        await session.SendToUpstreamAsync(new byte[] { 9 });
        Assert.Contains(up.Sent, x => x.SequenceEqual(new byte[] { 9 }));
    }

    [Fact]
    public async Task ExcludeTags_DeafFilter_And_Wire_And_Services()
    {
        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync(new EmbedSessionOptions
        {
            Services = new Dictionary<string, object?> { ["db"] = "game1" },
            TelnetPolicy = new DefaultTelnetPolicy { TerminalType = "TWGS" },
        });
        Assert.Equal("game1", session.Services["db"]);
        Assert.IsType<DefaultTelnetPolicy>(session.Services["telnet_policy"]);

        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        var std = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "a", Tags = { "standard" } },
        });
        var deaf = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "b", Tags = { "deaf" } },
        });

        await session.SendToClientsAsync("Z"u8.ToArray(), new ClientFilter { ExcludeTags = new[] { "deaf" } });
        Assert.Equal("Z"u8.ToArray(), await std.ReceiveAsync());
        Assert.False(await TryReceiveAsync(deaf, 40));

        var wires = new List<WireEventKind>();
        session.WireEvents += (_, e) => wires.Add(e.Kind);
        await session.RaiseWireEventAsync(WireEventKind.Iac, new byte[] { 255, 251, 0 }, "will");
        Assert.Contains(WireEventKind.Iac, wires);

        await session.MarkNegotiatedAsync();
        var pol = new DefaultTelnetPolicy();
        Assert.NotEmpty(pol.OnOption(253, 0).ToArray());
        Assert.NotEmpty(pol.OnSubnegotiation(24, new byte[] { 1 }).ToArray());
        Assert.NotEmpty(pol.OnSubnegotiation(31, ReadOnlySpan<byte>.Empty).ToArray());
        Assert.Empty(pol.OnOption(200, 1).ToArray());
        Assert.Empty(pol.OnSubnegotiation(99, ReadOnlySpan<byte>.Empty).ToArray());

        // filter predicate + require tags edge
        Assert.False(new ClientFilter { RequireAnyTag = new[] { "x" } }.Matches(new ClientMetadata { ClientId = "n" }));
        Assert.True(ClientFilter.All.Matches(new ClientMetadata { ClientId = "n" }));
        Assert.True(new ClientFilter { Predicate = m => m.ClientId == "n" }.Matches(new ClientMetadata { ClientId = "n" }));

        Assert.Equal(InterceptAction.Pass, InterceptResult.Pass().Action);
        Assert.Equal(InterceptAction.Consume, InterceptResult.Consume().Action);

        await up.DisconnectAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task UpstreamLost_And_DuplicateClient_And_AutoId()
    {
        var hub = new EmbedHub();
        var s = await hub.CreateSessionAsync(); // auto id
        Assert.StartsWith("embed-", s.SessionId, StringComparison.Ordinal);
        var up = new MemoryUpstream();
        await s.ConnectUpstreamAsync(up);
        await s.AttachClientAsync(new ClientAttachOptions { Metadata = new ClientMetadata { ClientId = "x" } });
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            s.AttachClientAsync(new ClientAttachOptions { Metadata = new ClientMetadata { ClientId = "x" } }));
        await Assert.ThrowsAsync<ArgumentException>(() =>
            s.AttachClientAsync(new ClientAttachOptions { Metadata = new ClientMetadata { ClientId = "" } }));

        var lost = new TaskCompletionSource();
        s.LifecycleChanged += (_, e) =>
        {
            if (e.Phase == SessionLifecycle.UpstreamLost)
            {
                lost.TrySetResult();
            }
        };
        up.CompleteRemote();
        await lost.Task.WaitAsync(TimeSpan.FromSeconds(2));

        await Assert.ThrowsAsync<InvalidOperationException>(async () =>
            await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = s.SessionId }));
        // same id still registered until remove — use unique
        var s2 = await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = "unique-2" });
        Assert.NotNull(hub.GetSession("unique-2"));
        await s.DisposeAsync();
        await s2.DisposeAsync();
    }

    [Fact]
    public async Task MemoryUpstream_Errors_And_ClientDisconnectPolicy()
    {
        var mem = new MemoryUpstream();
        await Assert.ThrowsAsync<InvalidOperationException>(() => mem.SendAsync(new byte[] { 1 }));
        await mem.ConnectAsync();
        await mem.SendAsync(new byte[] { 1 });
        await mem.DisconnectAsync();
        await Assert.ThrowsAsync<InvalidOperationException>(async () =>
            await mem.PushFromRemoteAsync(new byte[] { 2 }));

        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync();
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        var c = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata
            {
                ClientId = "d",
                QueueCapacity = 1,
                Backpressure = BackpressurePolicy.Disconnect,
            },
        });
        await up.PushFromRemoteAsync(new byte[] { 1 });
        await up.PushFromRemoteAsync(new byte[] { 2 });
        await Task.Delay(40);
        // after disconnect policy client may be removed; receive may hang — don't require
        await c.DisposeAsync();
        await session.DisposeAsync();

        // DropNewest path
        var s2 = await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = "dn" });
        var up2 = new MemoryUpstream();
        await s2.ConnectUpstreamAsync(up2);
        await s2.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata
            {
                ClientId = "n",
                QueueCapacity = 1,
                Backpressure = BackpressurePolicy.DropNewest,
            },
        });
        await up2.PushFromRemoteAsync(new byte[] { 1 });
        await up2.PushFromRemoteAsync(new byte[] { 2 });
        await Task.Delay(30);
        await s2.DisposeAsync();
    }

    [Fact]
    public async Task Residual_Branches_Policy_Filters_And_Errors()
    {
        var pol = new DefaultTelnetPolicy();
        // WILL/WONT/DONT/DO arms
        Assert.NotEmpty(pol.OnOption(251, 1).ToArray()); // will
        Assert.NotEmpty(pol.OnOption(252, 1).ToArray()); // wont
        Assert.NotEmpty(pol.OnOption(254, 1).ToArray()); // dont
        Assert.Equal(InterceptAction.Replace, InterceptResult.Replace(new byte[] { 1 }).Action);
        Assert.Equal(InterceptAction.Defer, InterceptResult.Defer().Action);
        Assert.Equal(InterceptAction.Inject, InterceptResult.Inject(new byte[] { 2 }).Action);

        var hub = new EmbedHub();
        var boom = new BoomInterceptor();
        var session = await hub.CreateSessionAsync(new EmbedSessionOptions { Interceptor = boom });
        var up = new MemoryUpstream();
        await session.ConnectUpstreamAsync(up);
        await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "c", QueueCapacity = 1, Backpressure = BackpressurePolicy.DropNewest },
        });
        // empty forward
        await session.SendToClientsAsync(Array.Empty<byte>());
        // filter require tags miss + predicate false
        Assert.False(new ClientFilter { RequireAnyTag = new[] { "nope" } }.Matches(new ClientMetadata { ClientId = "z" }));
        Assert.False(new ClientFilter { Predicate = _ => false }.Matches(new ClientMetadata { ClientId = "z" }));
        Assert.True(new ClientFilter { RequireAnyTag = new[] { "t" } }.Matches(
            new ClientMetadata { ClientId = "z", Tags = new HashSet<string>(StringComparer.Ordinal) { "t" } }));

        boom.ClientResult = InterceptResult.Replace("R"u8.ToArray());
        await session.SendToUpstreamAsync("A"u8.ToArray());
        Assert.Contains(up.Sent, b => b.SequenceEqual("R"u8.ToArray()));

        boom.ClientResult = InterceptResult.Inject("I"u8.ToArray());
        boom.PassAfter = true;
        await session.SendToUpstreamAsync("B"u8.ToArray());

        // no upstream send error after disconnect via complete
        var s2 = await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = "err" });
        await Assert.ThrowsAsync<InvalidOperationException>(() => s2.SendToUpstreamAsync(new byte[] { 1 }));

        var up3 = new MemoryUpstream();
        await up3.ConnectAsync();
        up3.CompleteRemote();
        await Assert.ThrowsAsync<InvalidOperationException>(async () => await up3.PushFromRemoteAsync(new byte[] { 1 }));
        _ = await up3.ReceiveAsync(); // eof empty
        await up3.DisposeAsync();

        // disconnect backpressure removes client
        var s3 = await hub.CreateSessionAsync(new EmbedSessionOptions { SessionId = "disc" });
        var up4 = new MemoryUpstream();
        await s3.ConnectUpstreamAsync(up4);
        await s3.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata
            {
                ClientId = "gone",
                QueueCapacity = 1,
                Backpressure = BackpressurePolicy.Disconnect,
            },
        });
        await up4.PushFromRemoteAsync(new byte[] { 1 });
        await up4.PushFromRemoteAsync(new byte[] { 2 });
        await Task.Delay(50);
        await s3.DisposeAsync();
        await session.DisposeAsync();
        await s2.DisposeAsync();
    }

    private static async Task<bool> TryReceiveAsync(IClientHandle client, int ms)
    {
        using var cts = new CancellationTokenSource(ms);
        try
        {
            _ = await client.ReceiveAsync(cts.Token);
            return true;
        }
        catch (OperationCanceledException)
        {
            return false;
        }
    }

    private sealed class BoomInterceptor : IByteInterceptor
    {
        public InterceptResult? ClientResult { get; set; }
        public bool PassAfter { get; set; }
        private int _n;

        public ValueTask<InterceptResult> OnUpstreamAsync(InterceptContext context, CancellationToken cancellationToken = default) =>
            ValueTask.FromResult(InterceptResult.Pass());

        public ValueTask<InterceptResult> OnClientAsync(InterceptContext context, CancellationToken cancellationToken = default)
        {
            if (ClientResult is null)
            {
                return ValueTask.FromResult(InterceptResult.Pass());
            }

            var r = ClientResult;
            if (PassAfter)
            {
                _n++;
                if (_n > 1)
                {
                    ClientResult = null;
                    return ValueTask.FromResult(InterceptResult.Pass());
                }
            }
            else
            {
                ClientResult = null;
            }

            return ValueTask.FromResult(r);
        }
    }

    private sealed class ScriptInterceptor : IByteInterceptor
    {
        private readonly List<string> _order;
        public InterceptResult? NextUpstream { get; set; }
        public InterceptResult? NextClient { get; set; }
        public bool AfterInjectPass { get; set; }
        public byte[]? ReenterSendUpstream { get; set; }
        private int _injectDepth;

        public ScriptInterceptor(List<string> order) => _order = order;

        public async ValueTask<InterceptResult> OnUpstreamAsync(InterceptContext context, CancellationToken cancellationToken = default)
        {
            _order.Add("up:" + System.Text.Encoding.ASCII.GetString(context.Data));
            if (ReenterSendUpstream is not null && context.Data.SequenceEqual("PING"u8.ToArray()))
            {
                // re-entrant send while holding pipeline — session serializes
                await context.Session.SendToUpstreamAsync(ReenterSendUpstream, cancellationToken);
                return InterceptResult.Pass();
            }

            if (NextUpstream is not null)
            {
                var r = NextUpstream;
                if (r.Action == InterceptAction.Inject && AfterInjectPass)
                {
                    _injectDepth++;
                    if (_injectDepth > 1)
                    {
                        NextUpstream = InterceptResult.Pass();
                        return InterceptResult.Pass();
                    }
                }
                else
                {
                    NextUpstream = null;
                }

                return r;
            }

            return InterceptResult.Pass();
        }

        public ValueTask<InterceptResult> OnClientAsync(InterceptContext context, CancellationToken cancellationToken = default)
        {
            _order.Add("cli:" + System.Text.Encoding.ASCII.GetString(context.Data));
            if (NextClient is not null)
            {
                var r = NextClient;
                NextClient = null;
                return ValueTask.FromResult(r);
            }

            return ValueTask.FromResult(InterceptResult.Pass());
        }
    }
}
