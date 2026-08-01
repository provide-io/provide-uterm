//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Diagnostics;
using Provide.Uterm.Fanout;
using Provide.Uterm.ServerAuth;
using Xunit;

namespace Provide.Uterm.Tests;

public sealed partial class FanoutExecutionTests
{
    [Fact]
    public void Store_Does_Not_Alias_Saved_Get_Or_List_Records()
    {
        var store = new InMemoryGroupStore();
        var original = new Group
        {
            GroupId = "g", Name = "original", CreatedBy = "owner", WorkerIds = ["w1"], Grants = ["alice"],
        };
        store.Save(original);
        original.Name = "mutated-input";
        original.WorkerIds[0] = "mutated-input-worker";
        original.Grants[0] = "mutated-input-grant";

        Assert.True(store.TryGet("g", out var fetched));
        fetched.Name = "mutated-get";
        fetched.WorkerIds[0] = "mutated-get-worker";
        fetched.Grants[0] = "mutated-get-grant";
        var listed = Assert.Single(store.ListForPrincipal("owner"));
        listed.Name = "mutated-list";
        listed.WorkerIds[0] = "mutated-list-worker";
        listed.Grants[0] = "mutated-list-grant";

        Assert.True(store.TryGet("g", out var stored));
        Assert.Equal("original", stored.Name);
        Assert.Equal(["w1"], stored.WorkerIds);
        Assert.Equal(["alice"], stored.Grants);
    }

    [Fact]
    public async Task Store_Grants_Distinct_Principals_Atomically_While_Listing()
    {
        var store = new InMemoryGroupStore();
        store.Save(new Group { GroupId = "g", CreatedBy = "owner" });
        var start = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var grants = Enumerable.Range(0, 100).Select(index => "member-" + index).ToArray();
        var grantTasks = grants.Select(grantee => Task.Run(async () =>
        {
            await start.Task;
            Assert.True(store.GrantAccess("g", grantee, "owner"));
        })).ToArray();
        var listTask = Task.Run(async () =>
        {
            await start.Task;
            for (var i = 0; i < 100; i++)
            {
                var listed = Assert.Single(store.ListForPrincipal("owner"));
                listed.Grants.Add("caller-only");
            }
        });

        start.TrySetResult();
        await Task.WhenAll(grantTasks.Append(listTask));

        Assert.True(store.TryGet("g", out var stored));
        Assert.Equal(grants.Order(), stored.Grants.Order());
        Assert.DoesNotContain("caller-only", stored.Grants);
    }

    [Theory]
    [InlineData("broadcast")]
    [InlineData("send")]
    public async Task Noncooperative_Observer_And_Worker_Tasks_Are_Bounded_By_One_Operation_Deadline(string stage)
    {
        var hub = new HangingStageHub(stage);
        var controller = NewController(hub, "parallel", ["w1"]);
        var clock = Stopwatch.StartNew();

        var pending = controller.SendAsync("g", "id", Admin("alice"), 5, 60);
        await hub.Started.Task.WaitAsync(TimeSpan.FromSeconds(1));
        var result = await pending.WaitAsync(TimeSpan.FromMilliseconds(500));

        Assert.True(clock.ElapsedMilliseconds < 400, $"operation took {clock.ElapsedMilliseconds}ms");
        Assert.Equal(["w1"], result.FailedSessions);
        await hub.CancellationObserved.Task.WaitAsync(TimeSpan.FromMilliseconds(100));
    }

    [Fact]
    public async Task Caller_Cancellation_Propagates_Through_Noncooperative_Work()
    {
        var hub = new HangingStageHub("send");
        var controller = NewController(hub, "parallel", ["w1"]);
        using var cancellation = new CancellationTokenSource();
        var pending = controller.SendAsync("g", "id", Admin("alice"), 5, 5000, cancellation.Token);
        await hub.Started.Task.WaitAsync(TimeSpan.FromSeconds(1));

        cancellation.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () =>
            await pending.WaitAsync(TimeSpan.FromMilliseconds(500)));
        await hub.CancellationObserved.Task.WaitAsync(TimeSpan.FromMilliseconds(100));
    }

    [Fact]
    public async Task Late_Fault_From_Abandoned_Task_Is_Observed()
    {
        var hub = new HangingStageHub("send");
        var observed = new TaskCompletionSource<Exception>(TaskCreationOptions.RunContinuationsAsynchronously);
        var controller = NewController(hub, "parallel", ["w1"], lateFaultObserver: error => observed.TrySetResult(error));

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 40)
            .WaitAsync(TimeSpan.FromMilliseconds(500));
        Assert.Equal(["w1"], result.FailedSessions);
        hub.Fault(new InvalidOperationException("late boom"));

        var error = await observed.Task.WaitAsync(TimeSpan.FromMilliseconds(500));
        Assert.Equal("late boom", error.Message);
    }

    [Fact]
    public async Task Sequential_Members_Share_One_Total_Response_Budget()
    {
        var hub = new SlowReadHub(55);
        var controller = NewController(hub, "sequential", ["w1", "w2", "w3"]);
        var clock = Stopwatch.StartNew();

        var result = await controller.SendAsync("g", "id", Admin("alice"), 1000, 80)
            .WaitAsync(TimeSpan.FromMilliseconds(500));

        Assert.True(clock.ElapsedMilliseconds < 250, $"sequential operation took {clock.ElapsedMilliseconds}ms");
        Assert.Contains("w2", result.FailedSessions);
        Assert.Contains("w3", result.FailedSessions);
        Assert.DoesNotContain("send:w3", hub.Trace);
    }

    [Fact]
    public async Task Parallel_Sends_All_Before_Collecting_And_Returns_Output()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "same", ["w2"] = "same" });
        var controller = NewController(hub, "parallel", ["w1", "w2"]);

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 100);

        Assert.All(result.Results, row => Assert.True(row.Ok));
        Assert.Equal(["same", "same"], result.Results.Select(row => row.OutputDelta));
        Assert.True(hub.Trace.IndexOf("send:w2") < hub.Trace.IndexOf("read:w1"));
    }

    [Fact]
    public async Task Parallel_Subscription_Preparation_Failure_Is_Isolated_And_Closes_All_Opened_Handles_Once()
    {
        var hub = new PreparationFailureHub("w2");
        var controller = NewController(hub, "parallel", ["w1", "w2", "w3"]);

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 100);

        Assert.Equal(["w2"], result.FailedSessions);
        Assert.Equal(["w1", "w3"], hub.SentWorkers);
        Assert.DoesNotContain("w2", hub.SentWorkers);
        Assert.Equal(["w1", "w2", "w3"], hub.SubscriptionAttempts);
        Assert.Equal(1, hub.DisposeCounts["w1"]);
        Assert.Equal(1, hub.DisposeCounts["w3"]);
    }

    [Fact]
    public async Task Sequential_Subscription_Preparation_Failure_Is_Isolated_And_Continues()
    {
        var hub = new PreparationFailureHub("w2");
        var controller = NewController(hub, "sequential", ["w1", "w2", "w3"]);

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 100);

        Assert.Equal(["w2"], result.FailedSessions);
        Assert.Equal(["w1", "w3"], hub.SentWorkers);
        Assert.Equal(["w1", "w2", "w3"], hub.SubscriptionAttempts);
        Assert.Equal(1, hub.DisposeCounts["w1"]);
        Assert.Equal(1, hub.DisposeCounts["w3"]);
    }

    [Fact]
    public async Task Sequential_Collects_Each_Before_Sending_Next_And_Stops_On_Error()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "ERROR", ["w2"] = "never" });
        var controller = NewController(hub, "sequential", ["w1", "w2"], stopOnError: true);

        var result = await controller.SendAsync("g", "deploy", Admin("alice"), 5, 100);

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

        var result = await controller.SendAsync("g", "status", Admin("alice"), 50, 100);

        Assert.Contains("w3", result.DivergentSessions);
        Assert.All(result.Results, row => Assert.InRange(row.ElapsedMs, 0, 250));
    }

    [Fact]
    public async Task Authorized_Send_Never_Delivers_To_Refused_Members()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "forbidden" });
        var authorizer = new TestAuthorizer { DeniedMembers = ["w1"] };
        var controller = NewController(hub, "parallel", ["w1"], authorizer: authorizer);

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 100);

        Assert.DoesNotContain("send:w1", hub.Trace);
        Assert.Equal(["w1"], result.FailedSessions);
    }

    [Fact]
    public async Task Send_Cannot_Expand_Stored_Membership()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["outside"] = "unexpected" });
        var authorizer = new TestAuthorizer();
        var controller = NewController(hub, "parallel", ["w1"], authorizer: authorizer);

        _ = await controller.SendAsync("g", "id", Admin("alice"), 5, 100);

        Assert.DoesNotContain("send:outside", hub.Trace);
        Assert.Equal(["w1"], authorizer.CheckedMembers);
        Assert.DoesNotContain(typeof(Controller).GetMethods(), method => method.Name == "SendAuthorizedAsync");
    }

    [Fact]
    public async Task Send_Fails_Closed_For_Missing_Dependencies_And_Invalid_Principals()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "unexpected" });
        var withoutAuthorizer = new Controller(hub, new ControllerConfig { IdGen = () => "send" });
        withoutAuthorizer.CreateGroup(new Group { GroupId = "g", WorkerIds = ["w1"] }, "alice");
        await Assert.ThrowsAsync<FanoutAuthorizationException>(() =>
            withoutAuthorizer.SendAsync("g", "id", Admin("alice"), 5, 100));

        var controller = NewController(hub, "parallel", ["w1"]);
        await Assert.ThrowsAsync<FanoutAuthorizationException>(() =>
            controller.SendAsync("g", "id", null, 5, 100));
        await Assert.ThrowsAsync<FanoutAuthorizationException>(() =>
            controller.SendAsync("g", "id", Principal.Anonymous(), 5, 100));
        await Assert.ThrowsAsync<FanoutAuthorizationException>(() =>
            controller.SendAsync("g", "id", new Principal
            {
                SubjectId = "alice", Roles = StringSet.Of("viewer"), Scopes = StringSet.Of("*"),
            }, 5, 100));
        await Assert.ThrowsAsync<FanoutAuthorizationException>(() =>
            controller.SendAsync("g", "id", new Principal
            {
                SubjectId = "alice", Roles = StringSet.Of("admin"), Scopes = StringSet.Of("*"),
                AdminSessionScope = "w1",
            }, 5, 100));
        Assert.DoesNotContain(hub.Trace, entry => entry.StartsWith("send:", StringComparison.Ordinal));
    }

    [Fact]
    public async Task Throwing_Global_Admin_Authorizer_Is_Typed_And_Has_No_Delivery_Side_Effects()
    {
        await AssertThrowingAuthorizerFailsBeforeDelivery("global");
    }

    [Fact]
    public async Task Throwing_Member_Authorizer_Is_Typed_And_Has_No_Delivery_Side_Effects()
    {
        await AssertThrowingAuthorizerFailsBeforeDelivery("member");
    }

    [Theory]
    [InlineData("global")]
    [InlineData("member")]
    public async Task Already_Typed_Authorizer_Failures_Propagate_Without_Delivery(string stage)
    {
        var hub = new SideEffectTrackingHub();
        var expected = new FanoutAuthorizationException(stage + " refused");
        var controller = NewController(
            hub, "parallel", ["w1"], authorizer: new TypedThrowingAuthorizer(stage, expected));

        var actual = await Assert.ThrowsAsync<FanoutAuthorizationException>(() =>
            controller.SendAsync("g", "id", Admin("alice"), 5, 100));

        Assert.Same(expected, actual);
        Assert.Equal(0, hub.SubscriptionCount);
        Assert.Equal(0, hub.ObserverCount);
        Assert.Equal(0, hub.SendCount);
    }

    [Fact]
    public async Task Sequential_Refused_Send_Is_Failed_And_Later_Members_Continue()
    {
        var hub = new RefusingSendHub("w1");
        var controller = NewController(hub, "sequential", ["w1", "w2"]);

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 100);

        Assert.Equal(["w1"], result.FailedSessions);
        Assert.Equal(["w1", "w2"], hub.SendAttempts);
        Assert.True(Assert.Single(result.Results, row => row.WorkerId == "w2").Ok);
    }

    [Theory]
    [InlineData("parallel")]
    [InlineData("sequential")]
    public async Task Caller_Cancellation_During_Output_Collection_Propagates(string mode)
    {
        var hub = new CancelDuringCollectionHub();
        var controller = NewController(hub, mode, ["w1"]);
        using var cancellation = new CancellationTokenSource();
        var pending = controller.SendAsync("g", "id", Admin("alice"), 5, 5000, cancellation.Token);
        await hub.ReadStarted.Task.WaitAsync(TimeSpan.FromSeconds(1));

        cancellation.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () =>
            await pending.WaitAsync(TimeSpan.FromSeconds(1)));
    }

    [Theory]
    [InlineData("sync_throw")]
    [InlineData("completed_fault")]
    [InlineData("delayed_fault")]
    [InlineData("delayed_success")]
    [InlineData("deadline")]
    [InlineData("expired_before_dispose")]
    public async Task Subscription_Disposal_Failures_Are_Bounded_And_Do_Not_Change_Delivery(string mode)
    {
        var observed = new TaskCompletionSource<Exception>(TaskCreationOptions.RunContinuationsAsynchronously);
        var hub = new DisposalFailureHub(mode);
        var controller = NewController(
            hub, "parallel", ["w1"], lateFaultObserver: error => observed.TrySetResult(error));

        var result = await controller.SendAsync(
            "g", "id", Admin("alice"), 1,
            mode is "deadline" or "expired_before_dispose" ? 20 : 500);

        Assert.True(Assert.Single(result.Results).Ok);
        Assert.Equal(1, hub.DisposeAttempts);
        if (mode is "deadline" or "expired_before_dispose")
        {
            hub.FaultDisposal();
            Assert.Equal("dispose failed", (await observed.Task.WaitAsync(TimeSpan.FromSeconds(1))).Message);
        }
    }

    [Fact]
    public async Task Parallel_Output_Read_Failure_Is_Isolated_As_A_Failed_Member()
    {
        var controller = NewController(new ReadFailureHub(), "parallel", ["w1"]);

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 100);

        Assert.Equal(["w1"], result.FailedSessions);
        Assert.False(Assert.Single(result.Results).Ok);
    }

    [Fact]
    public async Task A_Throwing_Late_Fault_Observer_Cannot_Escape_The_Continuation()
    {
        var hub = new HangingStageHub("send");
        var observerCalled = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var controller = NewController(hub, "parallel", ["w1"], lateFaultObserver: _ =>
        {
            observerCalled.TrySetResult();
            throw new InvalidOperationException("observer failed");
        });

        var result = await controller.SendAsync("g", "id", Admin("alice"), 5, 30);
        hub.Fault(new IOException("late worker fault"));

        await observerCalled.Task.WaitAsync(TimeSpan.FromSeconds(1));
        Assert.Equal(["w1"], result.FailedSessions);
    }

    private static async Task AssertThrowingAuthorizerFailsBeforeDelivery(string stage)
    {
        var hub = new SideEffectTrackingHub();
        var controller = NewController(hub, "parallel", ["w1"], authorizer: new ThrowingAuthorizer(stage));

        var error = await Assert.ThrowsAsync<FanoutAuthorizationException>(() =>
            controller.SendAsync("g", "id", Admin("alice"), 5, 100));

        Assert.Contains("authorization", error.Message, StringComparison.OrdinalIgnoreCase);
        Assert.Equal(0, hub.SubscriptionCount);
        Assert.Equal(0, hub.ObserverCount);
        Assert.Equal(0, hub.SendCount);
    }

    [Fact]
    public async Task Group_Grant_Does_Not_Bypass_Current_Member_Authorization()
    {
        var hub = new EventHub(new Dictionary<string, string> { ["w1"] = "unexpected" });
        var authorizer = new TestAuthorizer { DeniedMembers = ["w1"] };
        var controller = NewController(hub, "parallel", ["w1"], authorizer: authorizer);
        controller.GrantAccess("g", "bob", "alice");

        var result = await controller.SendAsync("g", "id", Admin("bob"), 5, 100);

        Assert.Equal(["w1"], result.FailedSessions);
        Assert.DoesNotContain("send:w1", hub.Trace);
        Assert.Equal(["w1"], authorizer.CheckedMembers);
    }
}
