//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Text;
using Provide.Uterm.Shell;

namespace Provide.Uterm.Tests.Shell;

public class UshellDispatcherTests
{
    private static string Join(ShellResult r) => string.Join("", r.Text);

    [Fact]
    public void Help_And_HelpDetail_And_Unknown()
    {
        var d = new CommandDispatcher();
        var help = Join(d.Dispatch("help"));
        Assert.Contains("ushell commands", help, StringComparison.Ordinal);
        Assert.Contains("fetch", help, StringComparison.Ordinal);

        var detail = Join(d.Dispatch("help kv"));
        Assert.Contains("kv list", detail, StringComparison.Ordinal);

        var missing = Join(d.Dispatch("help nosuch"));
        Assert.Contains("no help for", missing, StringComparison.Ordinal);

        var unk = Join(d.Dispatch("foobar"));
        Assert.Contains("unknown command", unk, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Clear_Exit_PyStub_EmptyCtrlC()
    {
        var d = new CommandDispatcher();
        Assert.Contains("\x1b[2J", Join(d.Dispatch("clear")), StringComparison.Ordinal);
        Assert.Contains("Goodbye", Join(d.Dispatch("exit")), StringComparison.Ordinal);
        Assert.Contains("Goodbye", Join(d.Dispatch("quit")), StringComparison.Ordinal);

        var pyEmpty = Join(d.Dispatch("py"));
        Assert.Contains("usage: py <expr>", pyEmpty, StringComparison.Ordinal);
        var pyStub = Join(d.Dispatch("py 1+1"));
        Assert.Contains("unavailable in the Go build", pyStub, StringComparison.Ordinal);

        Assert.Equal(ShellOutput.Prompt, Join(d.Dispatch("")));
        Assert.Equal(ShellOutput.Prompt, Join(d.Dispatch("\x03")));
    }

    [Fact]
    public async Task Kv_And_Storage_WithMemoryBindings()
    {
        var kv = new MemoryKvStore();
        var storage = new MemoryShellStorage();
        storage.Put("s1", "v1");
        var env = new MemoryShellEnv(registry: kv, attrs: new Dictionary<string, string> { ["SESSION_REGISTRY"] = "KV" });
        var ctx = new ShellContext { Env = env, Storage = storage };
        var d = new CommandDispatcher(ctx);

        Assert.Contains("SESSION_REGISTRY", Join(d.Dispatch("env")), StringComparison.Ordinal);

        Assert.Contains("no keys found", Join(await d.DispatchAsync("kv list")), StringComparison.Ordinal);
        Assert.Contains("set session:a", Join(await d.DispatchAsync("kv set a hello")), StringComparison.Ordinal);
        Assert.Contains("hello", Join(await d.DispatchAsync("kv get a")), StringComparison.Ordinal);
        Assert.Contains("session:a", Join(await d.DispatchAsync("kv list")), StringComparison.Ordinal);
        Assert.Contains("deleted", Join(await d.DispatchAsync("kv delete a")), StringComparison.Ordinal);
        Assert.Contains("key not found", Join(await d.DispatchAsync("kv get a")), StringComparison.Ordinal);

        Assert.Contains("s1", Join(await d.DispatchAsync("storage list")), StringComparison.Ordinal);
        Assert.Contains("v1", Join(await d.DispatchAsync("storage get s1")), StringComparison.Ordinal);
        Assert.Contains("key not found", Join(await d.DispatchAsync("storage get missing")), StringComparison.Ordinal);
        Assert.Contains("usage: storage", Join(await d.DispatchAsync("storage")), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Sessions_List_And_Kill()
    {
        var runtime = new MemoryDoNamespace();
        var env = new MemoryShellEnv(runtime: runtime);
        var sessions = new List<IReadOnlyDictionary<string, object?>>
        {
            new Dictionary<string, object?>
            {
                ["session_id"] = "s1",
                ["lifecycle_state"] = "ready",
                ["connector_type"] = "ushell",
                ["connected"] = true,
            },
        };
        var ctx = new ShellContext
        {
            Env = env,
            ListKvSessions = _ => Task.FromResult<IReadOnlyList<IReadOnlyDictionary<string, object?>>>(sessions),
        };
        var d = new CommandDispatcher(ctx);
        var list = Join(await d.DispatchAsync("sessions"));
        Assert.Contains("s1", list, StringComparison.Ordinal);
        Assert.Contains("live", list, StringComparison.Ordinal);

        Assert.Contains("kill signal sent", Join(await d.DispatchAsync("sessions kill s1")), StringComparison.Ordinal);
        Assert.Contains("s1", runtime.Killed);
        Assert.Contains("usage: sessions kill", Join(await d.DispatchAsync("sessions kill")), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Sessions_And_Kv_MissingBindings()
    {
        var d = new CommandDispatcher(new ShellContext());
        Assert.Contains("list_kv_sessions not available", Join(await d.DispatchAsync("sessions")), StringComparison.Ordinal);
        Assert.Contains("SESSION_REGISTRY", Join(await d.DispatchAsync("kv list")), StringComparison.Ordinal);
        Assert.Contains("storage not available", Join(await d.DispatchAsync("storage list")), StringComparison.Ordinal);
        Assert.Contains("SESSION_RUNTIME", Join(await d.DispatchAsync("sessions kill x")), StringComparison.Ordinal);
        Assert.Contains("(empty context)", Join(d.Dispatch("env")), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Fetch_Get_And_Usage()
    {
        using var handler = new StubHandler((req, _) =>
        {
            var body = Encoding.UTF8.GetBytes("{\"ok\":true}");
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new ByteArrayContent(body),
            });
        });
        using var http = new HttpClient(handler);
        var d = new CommandDispatcher(client: http);
        Assert.Contains("usage: fetch", Join(await d.DispatchAsync("fetch")), StringComparison.Ordinal);
        var got = Join(await d.DispatchAsync("fetch http://example.test/x"));
        Assert.Contains("HTTP 200", got, StringComparison.Ordinal);
        Assert.Contains("ok", got, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Cast_FromFile_And_Render_Injected()
    {
        var castPath = Path.Combine(Path.GetTempPath(), "uterm-cast-" + Guid.NewGuid().ToString("N") + ".cast");
        var cast = """
            {"version":2,"width":80,"height":24}
            [0.0,"o","hello"]
            [0.1,"o"," world"]
            """;
        await File.WriteAllTextAsync(castPath, cast.Replace("\n", "\r\n", StringComparison.Ordinal));
        try
        {
            var d = new CommandDispatcher();
            var r = await d.DispatchAsync("cast file://" + castPath);
            Assert.NotNull(r.Animated);
            Assert.True(r.Animated!.Frames.Count > 1);
            Assert.Equal(15.0, r.Animated.Fps);

            var d2 = new CommandDispatcher(renderImage: (_, _, _, _) =>
                (new[] { "FRAME" }, 0));
            var imgPath = Path.Combine(Path.GetTempPath(), "uterm-img-" + Guid.NewGuid().ToString("N") + ".bin");
            await File.WriteAllBytesAsync(imgPath, new byte[] { 1, 2, 3 });
            try
            {
                var one = Join(await d2.DispatchAsync("render file://" + imgPath));
                Assert.Contains("FRAME", one, StringComparison.Ordinal);
            }
            finally
            {
                File.Delete(imgPath);
            }

            var d3 = new CommandDispatcher(renderImage: (_, _, _, _) =>
                (new[] { "A", "B" }, 10));
            var img2 = Path.Combine(Path.GetTempPath(), "uterm-img2-" + Guid.NewGuid().ToString("N") + ".bin");
            await File.WriteAllBytesAsync(img2, new byte[] { 9 });
            try
            {
                var anim = await d3.DispatchAsync("render --loop file://" + img2);
                Assert.NotNull(anim.Animated);
                Assert.True(anim.Animated!.Loop);
                Assert.Equal(2, anim.Animated.Frames.Count);
            }
            finally
            {
                File.Delete(img2);
            }
        }
        finally
        {
            File.Delete(castPath);
        }
    }

    [Fact]
    public async Task Cast_Errors_And_Render_Usage()
    {
        var d = new CommandDispatcher();
        Assert.Contains("usage: cast", Join(await d.DispatchAsync("cast")), StringComparison.Ordinal);
        Assert.Contains("usage: render", Join(await d.DispatchAsync("render")), StringComparison.Ordinal);
        Assert.Contains("unsupported URL scheme", Join(await d.DispatchAsync("cast ftp://x")), StringComparison.Ordinal);
        Assert.Contains("unknown flag", Join(await d.DispatchAsync("cast --bogus x")), StringComparison.Ordinal);
        Assert.Contains("unknown mode", Join(await d.DispatchAsync("render --mode 99 http://x")), StringComparison.Ordinal);
    }

    [Fact]
    public void FmtTable_Empty()
    {
        Assert.Contains("no results", ShellOutput.FmtTable(Array.Empty<IReadOnlyList<string>>(), null), StringComparison.Ordinal);
        var t = ShellOutput.FmtTable(new[] { new[] { "a", "b" } }, new[] { "A", "B" });
        Assert.Contains("A", t, StringComparison.Ordinal);
        Assert.Contains("a", t, StringComparison.Ordinal);
        Assert.Equal("ab", ShellOutput.PadRight("ab", 1));
    }

    [Fact]
    public async Task Kv_Usage_And_FetchPost()
    {
        var kv = new MemoryKvStore();
        var d = new CommandDispatcher(new ShellContext { Env = new MemoryShellEnv(registry: kv) });
        Assert.Contains("usage: kv get", Join(await d.DispatchAsync("kv get")), StringComparison.Ordinal);
        Assert.Contains("usage: kv set", Join(await d.DispatchAsync("kv set onlykey")), StringComparison.Ordinal);
        Assert.Contains("usage: kv delete", Join(await d.DispatchAsync("kv delete")), StringComparison.Ordinal);
        Assert.Contains("usage: kv list", Join(await d.DispatchAsync("kv")), StringComparison.OrdinalIgnoreCase);
        Assert.Contains("set session:z", Join(await d.DispatchAsync("kv set session:z val")), StringComparison.Ordinal);

        using var handler = new StubHandler(async (req, _) =>
        {
            var body = req.Content is null ? "" : await req.Content.ReadAsStringAsync();
            return new HttpResponseMessage(HttpStatusCode.Created)
            {
                Content = new StringContent("posted:" + body),
            };
        });
        using var http = new HttpClient(handler);
        var d2 = new CommandDispatcher(client: http);
        var post = Join(await d2.DispatchAsync("fetch -X POST http://example.test/p hello"));
        Assert.Contains("HTTP 201", post, StringComparison.Ordinal);
        Assert.Contains("posted:", post, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Render_Flag_Parse_Errors_And_EmptyImage()
    {
        var d = new CommandDispatcher(renderImage: (_, _, _, _) => (Array.Empty<string>(), 10));
        Assert.Contains("invalid --cols", Join(await d.DispatchAsync("render --cols xx http://x")), StringComparison.Ordinal);
        Assert.Contains("invalid --rows", Join(await d.DispatchAsync("render --rows xx http://x")), StringComparison.Ordinal);
        Assert.Contains("invalid --fps", Join(await d.DispatchAsync("render --fps xx http://x")), StringComparison.Ordinal);
        Assert.Contains("unknown flag", Join(await d.DispatchAsync("render --wat http://x")), StringComparison.Ordinal);
        Assert.Contains("file not found", Join(await new CommandDispatcher().DispatchAsync("render file:///no/such/image.bin")), StringComparison.Ordinal);
        // local file with no renderer
        var bare = Path.Combine(Path.GetTempPath(), "bare-" + Guid.NewGuid().ToString("N"));
        await File.WriteAllBytesAsync(bare, new byte[] { 0 });
        try
        {
            Assert.Contains("no renderer configured",
                Join(await new CommandDispatcher().DispatchAsync("render file://" + bare)),
                StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(bare);
        }

        var path = Path.Combine(Path.GetTempPath(), "empty-img-" + Guid.NewGuid().ToString("N"));
        await File.WriteAllBytesAsync(path, new byte[] { 1 });
        try
        {
            var d2 = new CommandDispatcher(renderImage: (_, _, _, _) => (Array.Empty<string>(), 10));
            Assert.Contains("empty image", Join(await d2.DispatchAsync("render file://" + path)), StringComparison.Ordinal);
            var d3 = new CommandDispatcher(renderImage: (_, _, _, _) => throw new InvalidOperationException("boom"));
            Assert.Contains("cannot decode image", Join(await d3.DispatchAsync("render file://" + path)), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public async Task Cast_More_Error_Paths()
    {
        var badHeader = Path.Combine(Path.GetTempPath(), "cast-bad-" + Guid.NewGuid().ToString("N") + ".cast");
        await File.WriteAllTextAsync(badHeader, "not-json\n");
        try
        {
            var d = new CommandDispatcher();
            Assert.Contains("invalid cast header", Join(await d.DispatchAsync("cast file://" + badHeader)), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(badHeader);
        }

        var badVer = Path.Combine(Path.GetTempPath(), "cast-ver-" + Guid.NewGuid().ToString("N") + ".cast");
        await File.WriteAllTextAsync(badVer, "{\"version\":1}\n[0,\"o\",\"x\"]\n");
        try
        {
            var d = new CommandDispatcher();
            Assert.Contains("unsupported asciicast version", Join(await d.DispatchAsync("cast file://" + badVer)), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(badVer);
        }

        var noEvents = Path.Combine(Path.GetTempPath(), "cast-empty-" + Guid.NewGuid().ToString("N") + ".cast");
        await File.WriteAllTextAsync(noEvents, "{\"version\":2}\n");
        try
        {
            var d = new CommandDispatcher();
            Assert.Contains("no output events", Join(await d.DispatchAsync("cast file://" + noEvents)), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(noEvents);
        }

        Assert.Contains("file not found", Join(await new CommandDispatcher().DispatchAsync("cast file:///no/such/cast.file")), StringComparison.Ordinal);
        Assert.Contains("invalid --fps", Join(await new CommandDispatcher().DispatchAsync("cast --fps xx u")), StringComparison.Ordinal);
    }

    [Fact]
    public void Env_Values_Fallback_And_SessionsEmpty()
    {
        var ctx = new ShellContext
        {
            Values = new Dictionary<string, object?> { ["alpha"] = 1, ["_hidden"] = 2 },
            ListKvSessions = _ => Task.FromResult<IReadOnlyList<IReadOnlyDictionary<string, object?>>>(
                Array.Empty<IReadOnlyDictionary<string, object?>>()),
        };
        var d = new CommandDispatcher(ctx);
        var env = Join(d.Dispatch("env"));
        Assert.Contains("alpha", env, StringComparison.Ordinal);
        Assert.DoesNotContain("_hidden", env, StringComparison.Ordinal);
        Assert.Contains("no sessions found", Join(d.Dispatch("sessions")), StringComparison.Ordinal);
    }

    [Fact]
    public void StrUtil_And_Output_Edge()
    {
        Assert.Equal("a  ", ShellOutput.PadRight("a", 3));
        Assert.Contains("—", ShellOutput.Banner, StringComparison.Ordinal);
        Assert.Contains("❯", ShellOutput.Prompt, StringComparison.Ordinal);

        // InternalsVisibleTo — hit residual StrUtil / LineBuffer arms.
        Assert.Null(StrUtil.PySplit1("   \t  "));
        Assert.Equal("x", StrUtil.PyStrip("\f\vx\v\f"));
        Assert.False(StrUtil.Truthy(null));
        Assert.False(StrUtil.Truthy(false));
        Assert.False(StrUtil.Truthy(""));
        Assert.True(StrUtil.Truthy("z"));
        Assert.False(StrUtil.Truthy(0));
        Assert.True(StrUtil.Truthy(1));
        Assert.False(StrUtil.Truthy(0L));
        Assert.False(StrUtil.Truthy(0.0));
        Assert.False(StrUtil.Truthy(0f));
        Assert.True(StrUtil.Truthy(new object()));

        var lb = new LineBuffer();
        Assert.Null(lb.Feed("\x1b[1;2A")); // CSI with params (loop arm)
        Assert.Null(lb.Feed("ab"));
        Assert.Null(lb.Feed("\x04")); // Ctrl-D with buffer: ignore
        Assert.Equal("ab", lb.Feed("\r"));
    }

    [Fact]
    public async Task Branch_Coverage_Exceptions_And_Http()
    {
        // Sessions / kill / kv / storage exception paths
        var badLister = new ShellContext
        {
            ListKvSessions = _ => throw new InvalidOperationException("list boom"),
        };
        Assert.Contains("list boom", Join(await new CommandDispatcher(badLister).DispatchAsync("sessions")), StringComparison.Ordinal);

        var boomDo = new BoomDoNamespace();
        var killCtx = new ShellContext { Env = new MemoryShellEnv(runtime: boomDo) };
        Assert.Contains("kill boom", Join(await new CommandDispatcher(killCtx).DispatchAsync("sessions kill x")), StringComparison.Ordinal);

        var boomKv = new BoomKvStore();
        var kvCtx = new ShellContext { Env = new MemoryShellEnv(registry: boomKv) };
        Assert.Contains("kv boom", Join(await new CommandDispatcher(kvCtx).DispatchAsync("kv list")), StringComparison.Ordinal);
        Assert.Contains("usage: kv set", Join(await new CommandDispatcher(kvCtx).DispatchAsync("kv set")), StringComparison.Ordinal);

        var boomStorage = new BoomShellStorage();
        var stCtx = new ShellContext { Storage = boomStorage };
        Assert.Contains("st boom", Join(await new CommandDispatcher(stCtx).DispatchAsync("storage list")), StringComparison.Ordinal);
        Assert.Contains("usage: storage get", Join(await new CommandDispatcher(stCtx).DispatchAsync("storage get")), StringComparison.Ordinal);
        Assert.Contains("no storage keys", Join(await new CommandDispatcher(
            new ShellContext { Storage = new MemoryShellStorage() }).DispatchAsync("storage list")), StringComparison.Ordinal);

        // Idle session + truthy defaults
        var idleSessions = new List<IReadOnlyDictionary<string, object?>>
        {
            new Dictionary<string, object?>
            {
                ["session_id"] = "idle1",
                ["lifecycle_state"] = "paused",
                ["connector_type"] = "pty",
                ["connected"] = false,
            },
        };
        var idleCtx = new ShellContext
        {
            ListKvSessions = _ => Task.FromResult<IReadOnlyList<IReadOnlyDictionary<string, object?>>>(idleSessions),
        };
        Assert.Contains("idle", Join(await new CommandDispatcher(idleCtx).DispatchAsync("sessions")), StringComparison.Ordinal);

        // Fetch usage edges + status colors + error + truncation
        Assert.Contains("usage: fetch", Join(await new CommandDispatcher().DispatchAsync("fetch -X")), StringComparison.Ordinal);
        Assert.Contains("usage: fetch", Join(await new CommandDispatcher().DispatchAsync("fetch -X POST")), StringComparison.Ordinal);

        using var colorHandler = new StubHandler((req, _) =>
        {
            var code = req.RequestUri!.AbsolutePath.Contains("500", StringComparison.Ordinal)
                ? HttpStatusCode.InternalServerError
                : HttpStatusCode.NotFound;
            return Task.FromResult(new HttpResponseMessage(code)
            {
                Content = new StringContent(new string('x', 900)),
            });
        });
        using var colorHttp = new HttpClient(colorHandler);
        var dColor = new CommandDispatcher(client: colorHttp);
        Assert.Contains("HTTP 404", Join(await dColor.DispatchAsync("fetch http://example.test/404")), StringComparison.Ordinal);
        Assert.Contains("HTTP 500", Join(await dColor.DispatchAsync("fetch http://example.test/500")), StringComparison.Ordinal);
        Assert.Contains("…", Join(await dColor.DispatchAsync("fetch http://example.test/404")), StringComparison.Ordinal);

        using var throwHandler = new StubHandler((_, _) => throw new HttpRequestException("net down"));
        using var throwHttp = new HttpClient(throwHandler);
        Assert.Contains("net down", Join(await new CommandDispatcher(client: throwHttp).DispatchAsync("fetch http://example.test/x")), StringComparison.Ordinal);

        // Render flag success arms + missing url after flags
        var imgPath = Path.Combine(Path.GetTempPath(), "cov-img-" + Guid.NewGuid().ToString("N") + ".bin");
        await File.WriteAllBytesAsync(imgPath, new byte[] { 1, 2 });
        try
        {
            var dR = new CommandDispatcher(renderImage: (_, _, _, mode) =>
                (new[] { "M:" + mode }, 12));
            Assert.Contains("M:256", Join(await dR.DispatchAsync("render --mode 256 --cols 40 --rows 12 --fps 5 file://" + imgPath)), StringComparison.Ordinal);
            Assert.Contains("usage: render", Join(await dR.DispatchAsync("render --loop --fps 3")), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(imgPath);
        }

        // HTTP fetch via render (hits FetchBytes maxRead=0 + User-Agent)
        using var httpImg = new StubHandler((_, _) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new ByteArrayContent(new byte[] { 9, 9 }),
            }));
        using var httpImgClient = new HttpClient(httpImg);
        var dHttpRender = new CommandDispatcher(
            client: httpImgClient,
            renderImage: (_, _, _, _) => (new[] { "HTTPIMG" }, 0));
        Assert.Contains("HTTPIMG", Join(await dHttpRender.DispatchAsync("render http://example.test/i.png")), StringComparison.Ordinal);

        using var failHttp = new StubHandler((_, _) => throw new HttpRequestException("gone"));
        using var failClient = new HttpClient(failHttp);
        Assert.Contains("cannot fetch", Join(await new CommandDispatcher(client: failClient).DispatchAsync("render http://example.test/x")), StringComparison.Ordinal);

        // Unreadable file → cannot fetch (Unix only; Windows residual is acceptable)
        if (!OperatingSystem.IsWindows())
        {
            var locked = Path.Combine(Path.GetTempPath(), "locked-" + Guid.NewGuid().ToString("N"));
            await File.WriteAllBytesAsync(locked, new byte[] { 1 });
            try
            {
#pragma warning disable CA1416 // UnixFileMode is Unix-only; gated above
                File.SetUnixFileMode(locked, (UnixFileMode)0);
                var msg = Join(await new CommandDispatcher().DispatchAsync("render file://" + locked));
                Assert.True(
                    msg.Contains("cannot fetch", StringComparison.Ordinal) ||
                    msg.Contains("file not found", StringComparison.Ordinal) ||
                    msg.Contains("Permission", StringComparison.OrdinalIgnoreCase),
                    msg);
                File.SetUnixFileMode(locked, UnixFileMode.UserRead | UnixFileMode.UserWrite);
#pragma warning restore CA1416
            }
            finally
            {
                try { File.Delete(locked); } catch { /* best-effort */ }
            }
        }

        // Directory as file:// → file not found
        var dir = Path.Combine(Path.GetTempPath(), "ushell-dir-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            Assert.Contains("file not found", Join(await new CommandDispatcher().DispatchAsync("render file://" + dir)), StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(dir);
        }

        // Cast branch residuals: --loop, --fps, empty, non-object header, skip events, version string, idx clamp
        var emptyCast = Path.Combine(Path.GetTempPath(), "cast-empty2-" + Guid.NewGuid().ToString("N") + ".cast");
        await File.WriteAllTextAsync(emptyCast, "\n\n  \n");
        try
        {
            Assert.Contains("empty cast", Join(await new CommandDispatcher().DispatchAsync("cast file://" + emptyCast)), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(emptyCast);
        }

        var arrHeader = Path.Combine(Path.GetTempPath(), "cast-arr-" + Guid.NewGuid().ToString("N") + ".cast");
        await File.WriteAllTextAsync(arrHeader, "[1,2]\n[0,\"o\",\"x\"]\n");
        try
        {
            Assert.Contains("header is not an object", Join(await new CommandDispatcher().DispatchAsync("cast file://" + arrHeader)), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(arrHeader);
        }

        var mixed = Path.Combine(Path.GetTempPath(), "cast-mix-" + Guid.NewGuid().ToString("N") + ".cast");
        await File.WriteAllTextAsync(mixed, """
            {"version":"2"}
            [0,"i","input-skip"]
            [0.0,"o","a"]
            not-json-line
            [1]
            ["x","o","bad-ts"]
            [-1.0,"o","early"]
            [999.0,"o","late"]
            """);
        try
        {
            var anim = await new CommandDispatcher().DispatchAsync("cast --loop --fps 10 file://" + mixed);
            Assert.NotNull(anim.Animated);
            Assert.True(anim.Animated!.Loop);
            Assert.Equal(10.0, anim.Animated.Fps);
            Assert.True(anim.Animated.Frames.Count > 1);
        }
        finally
        {
            File.Delete(mixed);
        }

        // Version missing → unsupported
        var noVer = Path.Combine(Path.GetTempPath(), "cast-nover-" + Guid.NewGuid().ToString("N") + ".cast");
        await File.WriteAllTextAsync(noVer, "{}\n[0,\"o\",\"x\"]\n");
        try
        {
            Assert.Contains("unsupported asciicast version", Join(await new CommandDispatcher().DispatchAsync("cast file://" + noVer)), StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(noVer);
        }

        // Ctrl-D dispatch + help detail for cast
        var d = new CommandDispatcher();
        Assert.Contains("Goodbye", Join(d.Dispatch("\x04")), StringComparison.Ordinal);
        Assert.Contains("asciicast", Join(d.Dispatch("help cast")), StringComparison.Ordinal);
    }

    private sealed class StubHandler : HttpMessageHandler
    {
        private readonly Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> _fn;

        public StubHandler(Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> fn) => _fn = fn;

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            _fn(request, cancellationToken);
    }

    private sealed class BoomDoNamespace : IDoNamespace
    {
        public Task KillAsync(string sessionId, CancellationToken ct = default) =>
            throw new InvalidOperationException("kill boom");
    }

    private sealed class BoomKvStore : IKvStore
    {
        public Task<IReadOnlyList<string>> ListAsync(string prefix, CancellationToken ct = default) =>
            throw new InvalidOperationException("kv boom");

        public Task<string?> GetAsync(string key, CancellationToken ct = default) =>
            throw new InvalidOperationException("kv boom");

        public Task PutAsync(string key, string value, CancellationToken ct = default) =>
            throw new InvalidOperationException("kv boom");

        public Task DeleteAsync(string key, CancellationToken ct = default) =>
            throw new InvalidOperationException("kv boom");
    }

    private sealed class BoomShellStorage : IShellStorage
    {
        public Task<IReadOnlyList<string>> ListAsync(CancellationToken ct = default) =>
            throw new InvalidOperationException("st boom");

        public Task<string?> GetAsync(string key, CancellationToken ct = default) =>
            throw new InvalidOperationException("st boom");
    }
}
