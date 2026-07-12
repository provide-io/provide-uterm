//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Ansi;
using Provide.Uterm.Cli;
using Provide.Uterm.Frames;
using Provide.Uterm.Hub;
using Provide.Uterm.Manager;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.Session;

namespace Provide.Uterm.Tests;

/// <summary>Wave 6: WaitCancel hooks + pure residual surfaces toward 97%.</summary>
public class CoverageTo97Wave6Tests : IDisposable
{
    private readonly Action _prevRootWait;
    private readonly Action _prevMgrWait;

    public CoverageTo97Wave6Tests()
    {
        _prevRootWait = Root.WaitForCancel;
        _prevMgrWait = ManagerProgram.WaitForCancel;
        Root.WaitForCancel = () => { /* no-op for tests */ };
        ManagerProgram.WaitForCancel = () => { /* no-op for tests */ };
    }

    public void Dispose()
    {
        Root.WaitForCancel = _prevRootWait;
        ManagerProgram.WaitForCancel = _prevMgrWait;
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }

    [Fact]
    public void Cli_NonOnce_Paths_Via_WaitForCancel_Hook()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();

        // listen without --once (hits WaitCancel then stop) — needs WS_URL
        Assert.Equal(0, Root.Execute(
            new[] { "listen", "ws://127.0.0.1:9/ws", "--host", "127.0.0.1", "--port", FreePort().ToString() },
            o, e));

        // share / inspect without --once
        Assert.Equal(0, Root.Execute(new[] { "share", "--command", "true" }, o, e));
        Assert.Equal(0, Root.Execute(
            new[] { "inspect", "--upstream", "http://127.0.0.1:9", "--host", "127.0.0.1", "--port", "0" },
            o, e));

        // server without --once (start, WaitCancel no-op, stop)
        Assert.Equal(0, Root.Execute(
            new[] { "server", "--host", "127.0.0.1", "--port", FreePort().ToString() },
            o, e));
    }

    [Fact]
    public async Task ManagerProgram_Run_With_WaitForCancel_Hook()
    {
        var port = FreePort();
        var code = await ManagerProgram.RunAsync(new[]
        {
            "--host", "127.0.0.1", "--port", port.ToString(), "--token", "t",
        });
        Assert.Equal(0, code);
    }

    [Fact]
    public void Frames_WriteValue_IDictionary_And_IEnumerable()
    {
        // IDictionary<string,object?> that is NOT IReadOnlyDictionary
        var d = new DictOnly { ["a"] = 1, ["b"] = "x" };
        var bytes = FrameCodec.JsonMarshal(new Dictionary<string, object?>
        {
            ["type"] = "x",
            ["d"] = d,
            ["ints"] = new Dictionary<string, int> { ["n"] = 3 },
            ["list"] = new ArrayList { 1, "two", null },
            ["obj"] = new object(), // default ToString path
        });
        Assert.NotEmpty(bytes);

        using var doc = JsonDocument.Parse("""{"n":1,"s":"a","a":[1,true,null],"o":{"k":2.5}}""");
        _ = FrameCodec.JsonToObject(doc.RootElement);
        // undefined kind raw text — force via Parse on incomplete? Number/True covered.
    }

    private sealed class DictOnly : IDictionary<string, object?>
    {
        private readonly Dictionary<string, object?> _i = new();
        public object? this[string key] { get => _i[key]; set => _i[key] = value; }
        public ICollection<string> Keys => _i.Keys;
        public ICollection<object?> Values => _i.Values;
        public int Count => _i.Count;
        public bool IsReadOnly => false;
        public void Add(string key, object? value) => _i.Add(key, value);
        public void Add(KeyValuePair<string, object?> item) => _i.Add(item.Key, item.Value);
        public void Clear() => _i.Clear();
        public bool Contains(KeyValuePair<string, object?> item) => _i.ContainsKey(item.Key);
        public bool ContainsKey(string key) => _i.ContainsKey(key);
        public void CopyTo(KeyValuePair<string, object?>[] array, int arrayIndex) =>
            ((ICollection<KeyValuePair<string, object?>>)_i).CopyTo(array, arrayIndex);
        public IEnumerator<KeyValuePair<string, object?>> GetEnumerator() => _i.GetEnumerator();
        public bool Remove(string key) => _i.Remove(key);
        public bool Remove(KeyValuePair<string, object?> item) => _i.Remove(item.Key);
        public bool TryGetValue(string key, out object? value) => _i.TryGetValue(key, out value);
        IEnumerator IEnumerable.GetEnumerator() => _i.GetEnumerator();
    }

    [Fact]
    public void Ansi_Upgrade_EdgePaths()
    {
        // empty SGR
        _ = Upgrade.UpgradeTo256("\x1b[m");
        // 38/48 passthrough
        _ = Upgrade.UpgradeTo256("\x1b[38;5;1m");
        // empty parts / non-int digits
        _ = Upgrade.UpgradeTo256("\x1b[;00;xyz;30m");
        // unknown map index still emits code
        _ = Upgrade.UpgradeTo256("\x1b[0;1;39m");
        // all empty after filter → match return
        _ = Upgrade.UpgradeTo256("\x1b[;;;m");
        _ = Upgrade.UpgradeToTruecolor("\x1b[31;41m hi {P1}{T2}");
        _ = Upgrade.UpgradeTo256("{P0}{T15}", AnsiConstants.DefaultPalette);
    }

    [Fact]
    public void ApiKeys_Clock_Expire_Revoke()
    {
        var store = new ApiKeyStore();
        var now = 1000.0;
        store.SetClock(() => now);
        var (raw, rec) = store.Create("k", expiresInS: 10);
        Assert.NotNull(store.Validate(raw));
        now = 2000; // expired
        Assert.Null(store.Validate(raw));
        Assert.False(store.Revoke("missing"));
        var (raw2, rec2) = store.Create("k2");
        Assert.True(store.Revoke(rec2.KeyId));
        Assert.Null(store.Validate(raw2));
        // wrong key
        Assert.Null(store.Validate("not-a-key"));
    }

    [Fact]
    public async Task Session_Expect_Match_And_Timeout()
    {
        var pos = new CursorPos(1, 2);
        Assert.Equal(1, pos.X);
        var pd = new PromptDetection
        {
            PromptId = "p",
            InputType = "text",
            IsIdle = true,
            KvData = new Dictionary<string, object?> { ["a"] = 1 },
            Extra = new Dictionary<string, object?> { ["b"] = 2 },
        };
        Assert.Equal("p", pd.PromptId);

        var fake = new FakeExpect("hello world");
        var ok = await Expect.SendAndExpectAsync(fake, "x", new ExpectOptions
        {
            ExpectText = "hello",
            Timeout = TimeSpan.FromSeconds(1),
        });
        Assert.True(ok.Matched);

        var re = await Expect.SendAndExpectAsync(fake, "y", new ExpectOptions
        {
            ExpectRegex = "wor+",
            Timeout = TimeSpan.FromMilliseconds(200),
        });
        Assert.True(re.Matched);

        // empty expect → immediate match
        var any = await Expect.SendAndExpectAsync(fake, "z", new ExpectOptions
        {
            Timeout = TimeSpan.FromMilliseconds(50),
        });
        Assert.True(any.Matched);

        // timeout no match
        fake.ScreenText = "zzz";
        var miss = await Expect.SendAndExpectAsync(fake, "q", new ExpectOptions
        {
            ExpectText = "never",
            Timeout = TimeSpan.FromMilliseconds(30),
        });
        Assert.False(miss.Matched);
    }

    private sealed class FakeExpect : IExpectSession
    {
        public string ScreenText;
        private int _seq;

        public FakeExpect(string screen) => ScreenText = screen;

        public Task SendAsync(string data, CancellationToken cancellationToken = default)
        {
            _seq++;
            return Task.CompletedTask;
        }

        public Snapshot Snapshot() => new() { Screen = ScreenText };

        public int ScreenChangeSeq() => _seq;

        public Task<bool> WaitForScreenChangeAsync(TimeSpan timeout, int since, CancellationToken cancellationToken = default)
        {
            _seq = since + 1;
            return Task.FromResult(true);
        }
    }

    [Fact]
    public void Root_DefaultWaitCancel_Is_Assignable()
    {
        // Ensure production default still callable (don't block — replace again)
        var called = false;
        Root.WaitForCancel = () => called = true;
        Root.WaitForCancel();
        Assert.True(called);
    }

    [Fact]
    public void Cli_Proxy_Without_Once_Uses_WaitCancel()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        // WaitForCancel is no-op → cancels RunAsync immediately
        Assert.Equal(0, Root.Execute(
            new[]
            {
                "proxy", "127.0.0.1", "23",
                "--port", FreePort().ToString(),
                "--bind", "127.0.0.1",
            },
            o, e));
    }

    [Fact]
    public void ManagerHost_Once_And_Interactive()
    {
        Assert.Equal(0, ManagerHost.Run(new[] { "--once", "--host", "127.0.0.1", "--port", FreePort().ToString() }));
        Assert.Equal(0, ManagerHost.Run(new[] { "--host", "127.0.0.1", "--port", FreePort().ToString() }));
        Assert.Equal(0, ManagerHost.Run(new[] { "-V" }));
    }

    [Fact]
    public void Principal_StringSet_And_Name()
    {
        var s = StringSet.Of("b", "a");
        Assert.True(s.Has("a"));
        Assert.Equal(new[] { "a", "b" }, s.Sorted());
        var p = new Principal
        {
            SubjectId = "u1",
            DisplayName = "User",
            AdminSessionScope = "all",
            Claims = new Dictionary<string, object?> { ["k"] = 1 },
        };
        Assert.Equal("User", p.Name);
        p.DisplayName = null;
        Assert.Equal("u1", p.Name);
        var anon = Principal.Anonymous();
        Assert.Equal("anonymous", anon.SubjectId);
    }

    [Fact]
    public void Hub_ILeaseHub_Facade_And_Defaults()
    {
        var hub = new TermHub(new TermHubConfig
        {
            MaxEventDataChars = 0, // clamp
            MaxWorkers = 0,
            RestAcquireRateLimitPerSec = 0,
            RestSendRateLimitPerSec = 0,
            MaxBufferChars = 0,
            DashboardHijackLeaseS = 0,
            EventDequeMaxlen = 0,
        });
        hub.Conn.RegisterWorker("w", new Echo());
        var st = hub.Registry.Get("w")!;
        Assert.False(hub.IsHijacked(st));
        Assert.False(hub.IsDashboardHijackActive(st));
        Assert.False(hub.HasValidRestLease(st));
        Assert.False(hub.CanSendInput(st, new object()));
        hub.Metric("x", 1);
        hub.NotifyHijackChanged("w", true, "op");
        hub.AppendEventAsync("w", "term").GetAwaiter().GetResult();
        hub.PruneIfIdleAsync("w").GetAwaiter().GetResult();
    }

    private sealed class Echo : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }

    [Fact]
    public void Audit_HeadMismatch_And_MakeRecord()
    {
        var r0 = AuditChain.MakeRecord(0, "");
        var r1 = AuditChain.MakeRecord(1, (string)r0["record_hash"]!);
        var head = new AuditChain.ExpectedHead { Seq = 99, Hash = "nope" };
        var bad = AuditChain.VerifyRecords(new[] { r0, r1 }, head);
        Assert.False(bad.Ok);

        _ = AuditChain.MakeRecord(2L, "abc", detail: new Dictionary<string, object?> { ["n"] = 1 });
        _ = AuditChain.MakeRecord(3, "abc", ts: 1.5, monoNs: 99);
    }

    [Fact]
    public void CtrlMsg_Builders_LinkPattern_Validation()
    {
        IReadOnlyList<IReadOnlyDictionary<string, object?>> Bad(params Dictionary<string, object?>[] e) => e;

        Assert.Throws<ArgumentException>(() =>
            Provide.Uterm.CtrlMsg.Builders.MakeLinkPatterns(Bad(new Dictionary<string, object?>
            {
                ["pattern"] = "x",
                ["action"] = "click",
                ["unknown"] = 1,
            })));
        Assert.Throws<ArgumentException>(() =>
            Provide.Uterm.CtrlMsg.Builders.MakeLinkPatterns(Bad(new Dictionary<string, object?>
            {
                ["action"] = "click",
            })));
        Assert.Throws<ArgumentException>(() =>
            Provide.Uterm.CtrlMsg.Builders.MakeLinkPatterns(Bad(new Dictionary<string, object?>
            {
                ["pattern"] = "x",
            })));
        Assert.Throws<ArgumentException>(() =>
            Provide.Uterm.CtrlMsg.Builders.MakeLinkPatterns(Bad(new Dictionary<string, object?>
            {
                ["pattern"] = 1,
                ["action"] = "click",
            })));
        Assert.Throws<ArgumentException>(() =>
            Provide.Uterm.CtrlMsg.Builders.MakeLinkPatterns(Bad(new Dictionary<string, object?>
            {
                ["pattern"] = "x",
                ["action"] = "nope",
            })));
        _ = Provide.Uterm.CtrlMsg.Builders.MakeLinkPatterns(Bad(new Dictionary<string, object?>
        {
            ["pattern"] = "x",
            ["action"] = "url",
            ["group"] = 1,
            ["payload"] = new Dictionary<string, object?> { ["a"] = 1 },
        }));
        _ = Provide.Uterm.CtrlMsg.Builders.MakePresenceUpdate("u1");
        _ = Provide.Uterm.CtrlMsg.Builders.MakePresenceUpdate("u1", new Dictionary<string, object?> { ["role"] = "viewer" });
    }
}
