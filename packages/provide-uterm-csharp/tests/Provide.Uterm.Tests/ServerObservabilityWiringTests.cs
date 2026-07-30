//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// The hub's observability callbacks have to be wired by whatever builds the
/// hub, or they are decoration.
///
/// Both existed and neither was connected in the hosted server:
/// <c>TermHubConfig.OnMetric</c> was never assigned by <c>CreateFromConfig</c>,
/// so every counter the hub emitted — including the worker-hello refusals added
/// specifically so an operator could see them — was dropped on the floor, while
/// the code that emitted them looked correct. <c>OnLog</c> was added later with
/// the same shape and the same problem.
///
/// A counter nobody collects is worse than no counter: it reads as evidence in
/// the source and produces nothing at runtime.
/// </summary>
public sealed class ServerObservabilityWiringTests
{
    private static UtermServerConfig Config() => new()
    {
        Auth = new AuthConfig { Mode = "dev_token" },
    };

    [Fact]
    public void AHubCounterReachesTheServersMetrics()
    {
        var (server, _) = ServerFactory.CreateFromConfig(Config());

        server.HubForTests.Metric("uterm_wiring_probe_total", 1);

        Assert.Contains("uterm_wiring_probe_total", server.MetricsForTests.Prometheus());
    }

    [Fact]
    public void ARefusedWorkerHelloIsCounted()
    {
        // The path that motivated this: a hello that would lower a decided mode
        // is refused, and an operator can only see it happening through the
        // counter.
        var (server, _) = ServerFactory.CreateFromConfig(Config());
        var hub = server.HubForTests;
        const string workerId = "w-observed";
        hub.Registry.Put(workerId, new WorkerTermState());
        var (ok, _) = hub.Router.SetInputMode(workerId, InputModes.Hijack);
        Assert.True(ok);

        Assert.False(hub.Conn.SetWorkerHello(workerId, InputModes.Open));

        Assert.Contains("worker_hello_mode_blocked_total", server.MetricsForTests.Prometheus());
    }

    [Fact]
    public void ARefusedWorkerHelloIsAlsoLogged()
    {
        // The counter says refusals are happening; the log says *which* worker,
        // which is what somebody debugging a stuck session actually needs.
        var log = new StringWriter();
        var (server, _) = ServerFactory.CreateFromConfig(Config(), logWriter: log);
        var hub = server.HubForTests;
        const string workerId = "w-logged";
        hub.Registry.Put(workerId, new WorkerTermState());
        var (ok, _) = hub.Router.SetInputMode(workerId, InputModes.Hijack);
        Assert.True(ok);

        Assert.False(hub.Conn.SetWorkerHello(workerId, InputModes.Open));

        var written = log.ToString();
        Assert.Contains("worker_hello_mode_blocked", written, StringComparison.Ordinal);
        Assert.Contains(workerId, written, StringComparison.Ordinal);
    }

    [Fact]
    public void TheLogCarriesItsLevel()
    {
        // A sink that received only the message could not filter, and these are
        // warnings rather than information.
        var log = new StringWriter();
        var (server, _) = ServerFactory.CreateFromConfig(Config(), logWriter: log);

        server.HubForTests.Log("warning", "uterm_wiring_probe");

        Assert.Contains("warning", log.ToString(), StringComparison.Ordinal);
        Assert.Contains("uterm_wiring_probe", log.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void ANullLogWriterIsNotAnError()
    {
        // The default is stderr, and a caller passing nothing must not have to
        // know that. A hub whose logging threw would take the session with it.
        var (server, _) = ServerFactory.CreateFromConfig(Config());

        server.HubForTests.Log("warning", "uterm_wiring_probe_default_sink");
    }
}
