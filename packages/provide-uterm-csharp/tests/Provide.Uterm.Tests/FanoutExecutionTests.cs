//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using Provide.Uterm.Fanout;
using Xunit;

namespace Provide.Uterm.Tests;

public sealed class FanoutExecutionTests
{
    [Fact]
    public async Task Parallel_Sends_All_Before_Collecting_And_Returns_Output()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "same", ["w2"] = "same" });
        var controller = NewController(hub, "parallel", ["w1", "w2"]);

        var result = await controller.SendAsync("g", "id", "alice", 5, 100);

        Assert.All(result.Results, row => Assert.True(row.Ok));
        Assert.Equal(["same", "same"], result.Results.Select(row => row.OutputDelta));
        Assert.True(hub.Trace.IndexOf("send:w2") < hub.Trace.IndexOf("read:w1"));
    }

    [Fact]
    public async Task Sequential_Collects_Each_Before_Sending_Next_And_Stops_On_Error()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "ERROR", ["w2"] = "never" });
        var controller = NewController(hub, "sequential", ["w1", "w2"], stopOnError: true);

        var result = await controller.SendAsync("g", "deploy", "alice", 5, 100);

        Assert.DoesNotContain("send:w2", hub.Trace);
        Assert.Equal(["w2"], result.FailedSessions);
        Assert.Equal("ERROR", result.Results[0].OutputDelta);
    }

    [Fact]
    public async Task Parallel_Applies_Divergence_And_Hard_Maximum()
    {
        var hub = new EventHub(new Dictionary<string, string>
        {
            ["w1"] = "same",
            ["w2"] = "same",
            ["w3"] = "different",
        });
        var controller = NewController(hub, "parallel", ["w1", "w2", "w3"], threshold: 0.8);

        var result = await controller.SendAsync("g", "status", "alice", 50, 100);

        Assert.Contains("w3", result.DivergentSessions);
        Assert.All(result.Results, row => Assert.InRange(row.ElapsedMs, 0, 250));
    }

    [Fact]
    public async Task Authorized_Send_Never_Delivers_To_Refused_Members()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "forbidden" });
        var controller = NewController(hub, "parallel", ["w1"]);

        var result = await controller.SendAuthorizedAsync("g", "id", "alice", [], ["w1"], 5, 100);

        Assert.DoesNotContain("send:w1", hub.Trace);
        Assert.Equal(["w1"], result.FailedSessions);
    }

    private static Controller NewController(
        IFanoutHub hub,
        string mode,
        List<string> workers,
        bool stopOnError = false,
        double threshold = 0.8)
    {
        var controller = new Controller(hub, new ControllerConfig { IdGen = () => "send" });
        controller.CreateGroup(new Group
        {
            GroupId = "g",
            WorkerIds = workers,
            Mode = mode,
            StopOnFirstError = stopOnError,
            ErrorPattern = "ERROR",
            QuiesceMs = 5,
            MaxResponseMs = 100,
            DivergenceThreshold = threshold,
        }, "alice");
        return controller;
    }

    private sealed class EventHub : IFanoutHub
    {
        private readonly IReadOnlyDictionary<string, string> _outputs;
        private readonly ConcurrentDictionary<string, EventSubscription> _subscriptions = new();
        public List<string> Trace { get; } = new();

        public EventHub(IReadOnlyDictionary<string, string> outputs) => _outputs = outputs;

        public Task<bool> SendWorkerAsync(
            string workerId,
            IReadOnlyDictionary<string, object?> msg,
            CancellationToken ct = default)
        {
            lock (Trace) Trace.Add("send:" + workerId);
            if (_outputs.TryGetValue(workerId, out var output))
            {
                _subscriptions[workerId].Enqueue(new FanoutOutputEvent("term", output));
            }
            return Task.FromResult(true);
        }

        public Task BroadcastAsync(
            string workerId,
            IReadOnlyDictionary<string, object?> msg,
            CancellationToken ct = default) => Task.CompletedTask;

        public IFanoutOutputSubscription SubscribeOutput(string workerId)
        {
            var sub = new EventSubscription(workerId, Trace);
            _subscriptions[workerId] = sub;
            return sub;
        }
    }

    private sealed class EventSubscription : IFanoutOutputSubscription
    {
        private readonly string _workerId;
        private readonly List<string> _trace;
        private readonly System.Threading.Channels.Channel<FanoutOutputEvent?> _events =
            System.Threading.Channels.Channel.CreateUnbounded<FanoutOutputEvent?>();

        public EventSubscription(string workerId, List<string> trace)
        {
            _workerId = workerId;
            _trace = trace;
        }

        public void Enqueue(FanoutOutputEvent item) => _events.Writer.TryWrite(item);

        public async ValueTask<FanoutOutputEvent?> ReadAsync(CancellationToken ct)
        {
            lock (_trace) _trace.Add("read:" + _workerId);
            return await _events.Reader.ReadAsync(ct);
        }

        public ValueTask DisposeAsync()
        {
            _events.Writer.TryComplete();
            return ValueTask.CompletedTask;
        }
    }
}
