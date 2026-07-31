//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Diagnostics;
using System.Security.Cryptography;
using System.Text.RegularExpressions;
using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Fanout;

public interface IFanoutHub
{
    Task<bool> SendWorkerAsync(string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default);
    Task BroadcastAsync(string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default);
    IFanoutOutputSubscription SubscribeOutput(string workerId) => EmptyFanoutOutputSubscription.Instance;
}

public interface IFanoutAuthorizer
{
    bool IsGlobalAdmin(Principal principal);
    bool CanReadMember(Principal principal, string workerId);
}

public sealed class FanoutAuthorizationException : InvalidOperationException
{
    public FanoutAuthorizationException(string message) : base(message) { }
    public FanoutAuthorizationException(string message, Exception innerException) : base(message, innerException) { }
}

public interface IGroupStore
{
    void Save(Group group);
    bool TryGet(string groupId, out Group group);
    void Delete(string groupId);
    bool GrantAccess(string groupId, string grantee, string principal);
    IReadOnlyList<Group> ListForPrincipal(string principal);
}

public sealed class InMemoryGroupStore : IGroupStore
{
    private readonly object _lock = new();
    private readonly Dictionary<string, Group> _groups = new();

    public void Save(Group group)
    {
        lock (_lock)
        {
            _groups[group.GroupId] = group.DeepClone();
        }
    }

    public bool TryGet(string groupId, out Group group)
    {
        lock (_lock)
        {
            if (!_groups.TryGetValue(groupId, out var stored))
            {
                group = null!;
                return false;
            }
            group = stored.DeepClone();
            return true;
        }
    }

    public void Delete(string groupId)
    {
        lock (_lock)
        {
            _groups.Remove(groupId);
        }
    }

    public bool GrantAccess(string groupId, string grantee, string principal)
    {
        lock (_lock)
        {
            if (!_groups.TryGetValue(groupId, out var group) || group.CreatedBy != principal)
            {
                return false;
            }
            if (!group.Grants.Contains(grantee)) group.Grants.Add(grantee);
            return true;
        }
    }

    public IReadOnlyList<Group> ListForPrincipal(string principal)
    {
        lock (_lock)
        {
            return _groups.Values
                .Where(g => g.CreatedBy == principal || g.Grants.Contains(principal))
                .Select(g => g.DeepClone())
                .ToList();
        }
    }
}

public sealed class ControllerConfig
{
    public IGroupStore? Store { get; set; }
    public IFanoutAuthorizer? Authorizer { get; set; }
    public int MaxGroupSize { get; set; } = 50;
    public Func<string>? IdGen { get; set; }
    public Action<Exception>? LateFaultObserver { get; set; }
}

/// <summary>
/// Fan-out controller: multiplex one operator's input to a group of worker sessions.
/// Port of packages/provide-uterm-go/fanout/controller.go (standard-execution path).
/// </summary>
public sealed class Controller
{
    private const int MaxErrorPatternLen = 200;
    private readonly IFanoutHub? _hub;
    private readonly IGroupStore _store;
    private readonly IFanoutAuthorizer? _authorizer;
    private readonly int _maxGroupSize;
    private readonly Func<string> _newId;
    private readonly Action<Exception>? _lateFaultObserver;

    public Controller(IFanoutHub? hub = null, ControllerConfig? cfg = null)
    {
        cfg ??= new ControllerConfig();
        _hub = hub;
        _store = cfg.Store ?? new InMemoryGroupStore();
        _authorizer = cfg.Authorizer;
        _maxGroupSize = cfg.MaxGroupSize <= 0 ? 50 : cfg.MaxGroupSize;
        _newId = cfg.IdGen ?? NewHexId;
        _lateFaultObserver = cfg.LateFaultObserver;
    }

    private static string NewHexId()
    {
        var b = new byte[16];
        RandomNumberGenerator.Fill(b);
        return Convert.ToHexString(b).ToLowerInvariant();
    }

    public string CreateGroup(Group group, string principal)
    {
        if (group.WorkerIds.Count > _maxGroupSize)
        {
            throw new ArgumentException($"Group size {group.WorkerIds.Count} exceeds max {_maxGroupSize}");
        }

        ValidateErrorPattern(group.ErrorPattern);
        if (string.IsNullOrEmpty(group.GroupId))
        {
            group.GroupId = _newId();
        }

        group.CreatedBy = principal;
        if (group.CreatedAt == 0)
        {
            group.CreatedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        }

        _store.Save(group);
        return group.GroupId;
    }

    public void DeleteGroup(string groupId, string principal)
    {
        if (AuthorizedGroup(groupId, principal) is not null)
        {
            _store.Delete(groupId);
        }
    }

    public Group? GetGroup(string groupId, string principal) => AuthorizedGroup(groupId, principal);

    public IReadOnlyList<Group> ListGroups(string principal) => _store.ListForPrincipal(principal);

    public void GrantAccess(string groupId, string grantee, string principal)
    {
        _store.GrantAccess(groupId, grantee, principal);
    }

    /// <summary>Broadcast input and collect bounded output using the group's advertised mode.</summary>
    public async Task<Result> SendAsync(
        string groupId,
        string data,
        Principal? principal,
        int quiesceMs = 0,
        int maxResponseMs = 0,
        CancellationToken ct = default)
    {
        if (principal is null || string.IsNullOrWhiteSpace(principal.SubjectId) ||
            string.Equals(principal.SubjectId, "anonymous", StringComparison.Ordinal))
        {
            throw new FanoutAuthorizationException("fanout requires an authenticated principal");
        }
        if (_authorizer is null)
        {
            throw new FanoutAuthorizationException("fanout member authorizer is unavailable");
        }
        bool isGlobalAdmin;
        try
        {
            isGlobalAdmin = _authorizer.IsGlobalAdmin(principal);
        }
        catch (FanoutAuthorizationException)
        {
            throw;
        }
        catch (Exception error)
        {
            throw new FanoutAuthorizationException("fanout global-admin authorization failed", error);
        }
        if (!isGlobalAdmin)
        {
            throw new FanoutAuthorizationException("fanout requires a global admin principal");
        }
        ct.ThrowIfCancellationRequested();
        var group = AuthorizedGroup(groupId, principal.SubjectId);
        if (group is null)
        {
            return EmptyResult(groupId, data);
        }
        var allowed = new List<string>();
        var refused = new List<string>();
        try
        {
            foreach (var workerId in group.WorkerIds)
            {
                if (_authorizer.CanReadMember(principal, workerId)) allowed.Add(workerId);
                else refused.Add(workerId);
            }
        }
        catch (FanoutAuthorizationException)
        {
            throw;
        }
        catch (Exception error)
        {
            throw new FanoutAuthorizationException("fanout member authorization failed", error);
        }
        var dispatchGroup = CopyGroupWithWorkers(group, allowed);
        var result = await SendGroupAsync(
            dispatchGroup, data, principal.SubjectId, quiesceMs, maxResponseMs, ct).ConfigureAwait(false);
        AddFailures(result, refused);
        return result;
    }

    private async Task<Result> SendGroupAsync(
        Group group,
        string data,
        string principal,
        int quiesceMs,
        int maxResponseMs,
        CancellationToken ct)
    {
        var sendId = _newId();
        var sentAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        var result = new Result
        {
            GroupId = group.GroupId,
            SendId = sendId,
            Command = data,
            SentAt = sentAt,
        };
        if (_hub is null)
        {
            AddFailures(result, group.WorkerIds);
            return result;
        }

        var qMs = quiesceMs > 0 ? quiesceMs : Math.Max(1, group.QuiesceMs);
        var mMs = maxResponseMs > 0 ? maxResponseMs : Math.Max(1, group.MaxResponseMs);
        using var budget = new OperationBudget(ct, mMs);
        if (string.Equals(group.Mode, "sequential", StringComparison.Ordinal))
        {
            await SendSequentialAsync(group, result, data, principal, qMs, budget, ct).ConfigureAwait(false);
        }
        else
        {
            await SendParallelAsync(group, result, data, principal, qMs, budget, ct).ConfigureAwait(false);
        }

        ApplySuccessfulDivergence(result, group.DivergenceThreshold);
        return result;
    }

    private static Group CopyGroupWithWorkers(Group group, IEnumerable<string> workerIds)
    {
        var copy = group.DeepClone();
        copy.WorkerIds = workerIds.ToList();
        return copy;
    }

    private Result EmptyResult(string groupId, string data) => new()
    {
        GroupId = groupId,
        SendId = _newId(),
        Command = data,
        SentAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
    };

    private async Task SendParallelAsync(
        Group group, Result result, string data, string principal, int quiesceMs,
        OperationBudget budget, CancellationToken callerCancellation)
    {
        var subscriptions = new IFanoutOutputSubscription?[group.WorkerIds.Count];
        try
        {
            for (var index = 0; index < group.WorkerIds.Count; index++)
            {
                try
                {
                    subscriptions[index] = _hub!.SubscribeOutput(group.WorkerIds[index]);
                }
                catch
                {
                    subscriptions[index] = null;
                }
            }

            var sends = group.WorkerIds.Select((wid, index) => subscriptions[index] is null
                ? Task.FromResult(false)
                : ExecuteParallelSendAsync(
                    group, result, wid, data, principal, budget, callerCancellation)).ToArray();
            var accepted = await Task.WhenAll(sends).ConfigureAwait(false);
            var collects = group.WorkerIds.Select((wid, index) => accepted[index]
                ? ExecuteParallelCollectAsync(wid, subscriptions[index]!, quiesceMs, budget, callerCancellation)
                : Task.FromResult(new SessionResult { WorkerId = wid, Ok = false })).ToArray();
            result.Results.AddRange(await Task.WhenAll(collects).ConfigureAwait(false));
            result.FailedSessions.AddRange(result.Results.Where(row => !row.Ok).Select(row => row.WorkerId));
        }
        finally
        {
            foreach (var subscription in subscriptions)
            {
                if (subscription is null) continue;
                await DisposeBoundedAsync(subscription, budget).ConfigureAwait(false);
            }
        }
    }

    private async Task<bool> ExecuteParallelSendAsync(
        Group group, Result result, string workerId, string data, string principal, OperationBudget budget,
        CancellationToken callerCancellation)
    {
        try
        {
            await AwaitBoundedAsync(
                token => NotifyAsync(group, result, workerId, data, principal, token), budget).ConfigureAwait(false);
            var accepted = await AwaitBoundedAsync(
                token => _hub!.SendWorkerAsync(workerId, InputFrame(data, result.SentAt), token), budget)
                .ConfigureAwait(false);
            return accepted;
        }
        catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            return false;
        }
    }

    private async Task<SessionResult> ExecuteParallelCollectAsync(
        string workerId, IFanoutOutputSubscription subscription, int quiesceMs,
        OperationBudget budget, CancellationToken callerCancellation)
    {
        try
        {
            return await CollectRowAsync(workerId, subscription, quiesceMs, budget).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
        {
            throw;
        }
        catch
        {
            return new SessionResult { WorkerId = workerId, Ok = false };
        }
    }

    private async Task SendSequentialAsync(
        Group group, Result result, string data, string principal, int quiesceMs,
        OperationBudget budget, CancellationToken callerCancellation)
    {
        var stopped = false;
        Regex? errorPattern = string.IsNullOrEmpty(group.ErrorPattern)
            ? null
            : new Regex(group.ErrorPattern, RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        for (var index = 0; index < group.WorkerIds.Count; index++)
        {
            var wid = group.WorkerIds[index];
            if (stopped)
            {
                AddFailures(result, [wid]);
                continue;
            }

            if (budget.IsExpired)
            {
                AddFailures(result, group.WorkerIds.Skip(index));
                break;
            }

            IFanoutOutputSubscription? subscription = null;
            try
            {
                subscription = _hub!.SubscribeOutput(wid);
                await AwaitBoundedAsync(
                    token => NotifyAsync(group, result, wid, data, principal, token), budget).ConfigureAwait(false);
                var ok = await AwaitBoundedAsync(
                    token => _hub.SendWorkerAsync(wid, InputFrame(data, result.SentAt), token), budget)
                    .ConfigureAwait(false);
                if (!ok)
                {
                    AddFailures(result, [wid]);
                    continue;
                }

                var row = await CollectRowAsync(wid, subscription, quiesceMs, budget).ConfigureAwait(false);
                result.Results.Add(row);
                if (!row.Ok) result.FailedSessions.Add(wid);
                if (group.StopOnFirstError && row.Ok && errorPattern is not null && errorPattern.IsMatch(row.OutputDelta ?? ""))
                {
                    stopped = true;
                }
            }
            catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
            {
                throw;
            }
            catch
            {
                AddFailures(result, [wid]);
            }
            finally
            {
                if (subscription is not null)
                {
                    await DisposeBoundedAsync(subscription, budget).ConfigureAwait(false);
                }
            }
        }
    }

    private async Task<SessionResult> CollectRowAsync(
        string workerId, IFanoutOutputSubscription subscription, int quiesceMs, OperationBudget budget)
    {
        var remainingMs = budget.RemainingMs;
        if (remainingMs <= 0) return new SessionResult { WorkerId = workerId, Ok = false };
        var (output, elapsed) = await AwaitBoundedAsync(
            token => OutputCollector.CollectAsync(subscription, quiesceMs, remainingMs, token), budget)
            .ConfigureAwait(false);
        return new SessionResult { WorkerId = workerId, Ok = true, OutputDelta = output, ElapsedMs = elapsed };
    }

    private Task NotifyAsync(Group group, Result result, string wid, string data, string principal, CancellationToken ct) =>
        _hub!.BroadcastAsync(wid, new Dictionary<string, object?>
        {
            ["type"] = "fanout_input",
            ["group_id"] = group.GroupId,
            ["send_id"] = result.SendId,
            ["command"] = data,
            ["from_principal"] = principal,
        }, ct);

    private async Task AwaitBoundedAsync(
        Func<CancellationToken, Task> start, OperationBudget budget)
    {
        budget.CallerCancellation.ThrowIfCancellationRequested();
        if (budget.IsExpired) throw new FanoutDeadlineExceededException();
        var task = start(budget.Token);
        try
        {
            await task.WaitAsync(budget.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            if (!task.IsCompleted) ObserveLateFault(task);
            budget.CallerCancellation.ThrowIfCancellationRequested();
            throw new FanoutDeadlineExceededException();
        }
    }

    private async Task<T> AwaitBoundedAsync<T>(
        Func<CancellationToken, Task<T>> start, OperationBudget budget)
    {
        budget.CallerCancellation.ThrowIfCancellationRequested();
        if (budget.IsExpired) throw new FanoutDeadlineExceededException();
        var task = start(budget.Token);
        try
        {
            return await task.WaitAsync(budget.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            if (!task.IsCompleted) ObserveLateFault(task);
            budget.CallerCancellation.ThrowIfCancellationRequested();
            throw new FanoutDeadlineExceededException();
        }
    }

    private async Task DisposeBoundedAsync(IFanoutOutputSubscription subscription, OperationBudget budget)
    {
        Task task;
        try
        {
            task = subscription.DisposeAsync().AsTask();
        }
        catch
        {
            return;
        }

        if (task.IsCompleted)
        {
            try { await task.ConfigureAwait(false); } catch { _ = task.Exception; }
            return;
        }
        if (budget.IsExpired)
        {
            ObserveLateFault(task);
            return;
        }
        try
        {
            await task.WaitAsync(budget.Token).ConfigureAwait(false);
        }
        catch
        {
            if (!task.IsCompleted) ObserveLateFault(task);
            else _ = task.Exception;
        }
    }

    private void ObserveLateFault(Task task)
    {
        _ = task.ContinueWith(completed =>
        {
            var aggregate = completed.Exception;
            if (aggregate is null) return;
            try
            {
                _lateFaultObserver?.Invoke(aggregate.InnerExceptions.Count == 1
                    ? aggregate.InnerExceptions[0]
                    : aggregate);
            }
            catch
            {
                // Observation hooks must never create another unobserved fault.
            }
        }, CancellationToken.None, TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously,
            TaskScheduler.Default);
    }

    private static Dictionary<string, object?> InputFrame(string data, double sentAt) => new()
    {
        ["type"] = "input",
        ["data"] = data,
        ["ts"] = sentAt,
    };

    private static void AddFailures(Result result, IEnumerable<string> workerIds)
    {
        foreach (var wid in workerIds)
        {
            result.Results.Add(new SessionResult { WorkerId = wid, Ok = false });
            result.FailedSessions.Add(wid);
        }
    }

    private static void ApplySuccessfulDivergence(Result result, double threshold)
    {
        var rows = result.Results.Where(row => row.Ok).ToList();
        if (rows.Count == 0) return;
        var flags = Divergence.ComputeDivergence(rows.Select(row => row.OutputDelta ?? "").ToList(), threshold);
        for (var i = 0; i < rows.Count; i++)
        {
            rows[i].Divergent = flags[i];
            if (flags[i]) result.DivergentSessions.Add(rows[i].WorkerId);
        }
    }

    /// <summary>
    /// Flag divergence across collected session outputs for a group send.
    /// </summary>
    public Result FlagDivergence(Result result, Group group)
    {
        var outputs = result.Results.Select(r => r.OutputDelta ?? "").ToList();
        var flags = Divergence.ComputeDivergence(outputs, group.DivergenceThreshold);
        for (var i = 0; i < flags.Length && i < result.Results.Count; i++)
        {
            result.Results[i].Divergent = flags[i];
            if (flags[i])
            {
                result.DivergentSessions.Add(result.Results[i].WorkerId);
            }
        }

        return result;
    }

    private Group? AuthorizedGroup(string groupId, string principal)
    {
        if (!_store.TryGet(groupId, out var g))
        {
            return null;
        }

        if (g.CreatedBy == principal || g.Grants.Contains(principal))
        {
            return g;
        }

        return null;
    }

    private static void ValidateErrorPattern(string pattern)
    {
        if (string.IsNullOrEmpty(pattern))
        {
            return;
        }

        if (pattern.Length > MaxErrorPatternLen)
        {
            throw new ArgumentException($"error_pattern exceeds max length {MaxErrorPatternLen}");
        }

        _ = new Regex(pattern, RegexOptions.Compiled, TimeSpan.FromSeconds(1));
    }

    private sealed class FanoutDeadlineExceededException : TimeoutException { }

    private sealed class OperationBudget : IDisposable
    {
        private readonly Stopwatch _clock = Stopwatch.StartNew();
        private readonly int _maxResponseMs;
        private readonly CancellationTokenSource _deadline;

        public OperationBudget(CancellationToken callerCancellation, int maxResponseMs)
        {
            CallerCancellation = callerCancellation;
            _maxResponseMs = Math.Max(1, maxResponseMs);
            _deadline = CancellationTokenSource.CreateLinkedTokenSource(callerCancellation);
            _deadline.CancelAfter(_maxResponseMs);
        }

        public CancellationToken CallerCancellation { get; }
        public CancellationToken Token => _deadline.Token;
        public int RemainingMs => Math.Max(0, _maxResponseMs - (int)_clock.ElapsedMilliseconds);
        public bool IsExpired =>
            RemainingMs <= 0 || (_deadline.IsCancellationRequested && !CallerCancellation.IsCancellationRequested);

        public void Dispose() => _deadline.Dispose();
    }
}
