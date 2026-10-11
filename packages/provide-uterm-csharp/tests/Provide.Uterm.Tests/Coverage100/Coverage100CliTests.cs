//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Reflection;
using Provide.Uterm.Cli;
using Provide.Uterm.Manager;
using Provide.Uterm.Platform;
using Provide.Uterm.Server;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// CLI and manager arms: the Ctrl-C wait, non-<c>--once</c> command paths, the
/// manager's fault path and the share/inspect helpers.
/// </summary>
[Collection("UTERM_TEST_MODE")]
public sealed class Coverage100CliTests
{
    private static ConsoleCancelEventArgs CtrlC() =>
        (ConsoleCancelEventArgs)Activator.CreateInstance(
            typeof(ConsoleCancelEventArgs),
            BindingFlags.NonPublic | BindingFlags.Instance,
            binder: null,
            args: new object[] { ConsoleSpecialKey.ControlC },
            culture: null)!;

    [Fact]
    public async Task Ctrl_c_wait_returns_once_the_keypress_arrives_and_consumes_it()
    {
        // The real subscription is harmless to add and remove in a test host.
        ConsoleCancelEventHandler noop = (_, _) => { };
        ConsoleCancel.Subscribe(noop);
        ConsoleCancel.Unsubscribe(noop);

        var savedSub = ConsoleCancel.Subscribe;
        var savedUnsub = ConsoleCancel.Unsubscribe;
        var subscribed = new TaskCompletionSource<ConsoleCancelEventHandler>(TaskCreationOptions.RunContinuationsAsynchronously);
        ConsoleCancelEventHandler? removed = null;
        ConsoleCancel.Subscribe = h => subscribed.TrySetResult(h);
        ConsoleCancel.Unsubscribe = h => removed = h;
        try
        {
            var waiting = Task.Run(ConsoleCancel.WaitForCtrlC);
            var handler = await subscribed.Task.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.False(waiting.IsCompleted);
            var args = CtrlC();
            handler(null, args);
            await waiting.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.True(args.Cancel);
            Assert.Same(handler, removed);
        }
        finally
        {
            ConsoleCancel.Subscribe = savedSub;
            ConsoleCancel.Unsubscribe = savedUnsub;
        }
    }

    private static T WithWait<T>(Action wait, Func<T> body)
    {
        var saved = Root.WaitForCancel;
        Root.WaitForCancel = wait;
        try
        {
            return body();
        }
        finally
        {
            Root.WaitForCancel = saved;
        }
    }

    [Fact]
    public void Listen_hands_each_telnet_client_to_the_gateway_pump()
    {
        var port = C100Server.FreePort();
        var o = new StringWriter();
        var code = WithWait(
            () =>
            {
                // While "running": a client connects; the pump cannot reach the
                // (closed) WebSocket upstream, so the gateway closes the client.
                using var c = new TcpClient();
                c.Connect(IPAddress.Loopback, port);
                var n = 0;
                try
                {
                    n = c.GetStream().Read(new byte[1], 0, 1);
                }
                catch (IOException)
                {
                }

                Assert.Equal(0, n);
            },
            () => Root.Execute(new[] { "listen", $"ws://127.0.0.1:{C100Server.FreePort()}/ws", "--host", "127.0.0.1", "--port", port.ToString() }, o, o));
        Assert.Equal(0, code);
        Assert.Contains("telnet gateway", o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Tunnel_without_once_waits_after_the_hello()
    {
        await using var s = await C100Server.StartAsync();
        var url = s.BaseUrl.Replace("http", "ws", StringComparison.Ordinal) + "/tunnel/c100-share";
        var waited = 0;
        var o = new StringWriter();
        var code = WithWait(
            () => waited++,
            // --token=<value>: the generated token is base64url and starts with
            // '-' one time in 64, which the space-separated form reads as a flag.
            () => Root.Execute(new[] { "tunnel", "--url", url, "--token=" + s.Config.Auth.WorkerBearerToken }, o, o));
        Assert.True(code == 0, o.ToString());
        Assert.Equal(1, waited);
        Assert.True(o.ToString().Contains("sent hello", StringComparison.Ordinal), o.ToString());
    }

    [Fact]
    public void Server_in_test_mode_prints_the_test_mode_banner()
    {
        var saved = Environment.GetEnvironmentVariable("UTERM_TEST_MODE");
        var savedPath = Environment.GetEnvironmentVariable("UTERM_DEV_TOKEN_PATH");
        var tokenPath = Path.Combine(Path.GetTempPath(), "c100-cli-token-" + Guid.NewGuid().ToString("N"));
        Environment.SetEnvironmentVariable("UTERM_TEST_MODE", "1");
        Environment.SetEnvironmentVariable("UTERM_DEV_TOKEN_PATH", tokenPath);
        try
        {
            var o = new StringWriter();
            Assert.Equal(0, Root.Execute(new[] { "server", "--host", "127.0.0.1", "--port", C100Server.FreePort().ToString(), "--once" }, o, o));
            Assert.Contains(TestModeBanner.Warning, o.ToString(), StringComparison.Ordinal);
        }
        finally
        {
            Environment.SetEnvironmentVariable("UTERM_TEST_MODE", saved);
            Environment.SetEnvironmentVariable("UTERM_DEV_TOKEN_PATH", savedPath);
            File.Delete(tokenPath);
        }
    }

    [Fact]
    public async Task Manager_handler_fault_answers_500_and_survives_a_500_that_cannot_be_written()
    {
        var mgr = new AgentManager(new ManagerConfig { Host = "127.0.0.1", Port = C100Server.FreePort() });
        await using var server = new ManagerServer(mgr);
        var statuses = new List<int>();
        server.WriteResponse = (ctx, status, _) =>
        {
            lock (statuses)
            {
                statuses.Add(status);
            }

            if (status == 500)
            {
                ctx.Response.Abort(); // the client sees the connection drop
            }

            throw new IOException("write failed");
        };
        await server.StartAsync();
        using var http = new HttpClient();
        // Whether the aborted response reaches the client as an error or an
        // empty reply depends on the listener implementation; the handler's
        // own path is what is asserted.
        try
        {
            using var _ = await http.GetAsync(server.BaseAddress + "/health");
        }
        catch (HttpRequestException)
        {
        }

        lock (statuses)
        {
            Assert.Equal(new[] { 200, 500 }, statuses);
        }
    }

    [Fact]
    public async Task Inspect_proxy_records_nothing_for_requests_it_cannot_forward()
    {
        // An upstream base that is not a URL: building the forward fails
        // before any network (or ambient HTTP proxy) is involved.
        await using var proxy = new HttpInspectProxy("not a url");
        await proxy.StartAsync();
        using var http = new HttpClient();
        // The forward fails and the response is aborted; how an aborted
        // HttpListener response surfaces to the client differs by platform
        // (connection reset on Windows, an empty reply from the managed
        // listener), so only the proxy's own record is asserted.
        try
        {
            using var _ = await http.GetAsync($"http://127.0.0.1:{proxy.Port}/x");
        }
        catch (HttpRequestException)
        {
        }

        Assert.Empty(proxy.Transactions);
    }

    [Fact]
    public async Task Share_session_launches_bare_commands_on_both_platform_arms()
    {
        var realWindows = OperatingSystem.IsWindows();
        var bare = realWindows ? "cmd" : "sh";
        var saved = OsPlatform.IsWindowsProbe;
        try
        {
            // The Windows arm: the bare name goes to Process.Start as is.
            OsPlatform.IsWindowsProbe = () => true;
            await using (var direct = new PtyShareSession(bare))
            {
                await direct.StartAsync();
                Assert.True(direct.Running);
            }

            // The Unix arm: the bare name is resolved through /usr/bin/env.
            OsPlatform.IsWindowsProbe = () => false;
            await using var viaEnv = new PtyShareSession(bare);
            if (realWindows)
            {
                await Assert.ThrowsAnyAsync<Exception>(() => viaEnv.StartAsync());
            }
            else
            {
                await viaEnv.StartAsync();
                Assert.True(viaEnv.Running);
            }
        }
        finally
        {
            OsPlatform.IsWindowsProbe = saved;
        }
    }
}
