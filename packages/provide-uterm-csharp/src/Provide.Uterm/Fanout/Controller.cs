//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace Provide.Uterm.Fanout;

public interface IFanoutHub
{
    Task<bool> SendWorkerAsync(string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default);
    Task BroadcastAsync(string workerId, IReadOnlyDictionary<string, object?> msg, CancellationToken ct = default);
    IFanoutOutputSubscription SubscribeOutput(string workerId) => EmptyFanoutOutputSubscription.Instance;
}

public interface IGroupStore
{
    void Save(Group group);
    bool TryGet(string groupId, out Group group);
    void Delete(string groupId);
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
            _groups[group.GroupId] = group;
        }
    }

    public bool TryGet(string groupId, out Group group)
    {
        lock (_lock)
        {
            return _groups.TryGetValue(groupId, out group!);
        }
    }

    public void Delete(string groupId)
    {
        lock (_lock)
        {
            _groups.Remove(groupId);
        }
    }

    public IReadOnlyList<Group> ListForPrincipal(string principal)
    {
        lock (_lock)
        {
            return _groups.Values
                .Where(g => g.CreatedBy == principal || g.Grants.Contains(principal))
                .ToList();
        }
    }
}

public sealed class ControllerConfig
{
    public IGroupStore? Store { get; set; }
    public int MaxGroupSize { get; set; } = 50;
    public Func<string>? IdGen { get; set; }
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
    private readonly int _maxGroupSize;
    private readonly Func<string> _newId;

    public Controller(IFanoutHub? hub = null, ControllerConfig? cfg = null)
    {
        cfg ??= new ControllerConfig();
        _hub = hub;
        _store = cfg.Store ?? new InMemoryGroupStore();
        _maxGroupSize = cfg.MaxGroupSize <= 0 ? 50 : cfg.MaxGroupSize;
        _newId = cfg.IdGen ?? NewHexId;
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
        if (!_store.TryGet(groupId, out var g) || g.CreatedBy != principal)
        {
            return;
        }

        if (!g.Grants.Contains(grantee))
        {
            g.Grants.Add(grantee);
            _store.Save(g);
        }
    }

    /// <summary>Broadcast input and collect bounded output using the group's advertised mode.</summary>
    public async Task<Result> SendAsync(
        string groupId,
        string data,
        string principal,
        int quiesceMs = 0,
        int maxResponseMs = 0,
        CancellationToken ct = default)
    {
        var group = AuthorizedGroup(groupId, principal);
        if (group is null)
        {
            return EmptyResult(groupId, data);
        }
        return await SendGroupAsync(group, data, principal, quiesceMs, maxResponseMs, ct).ConfigureAwait(false);
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
        if (string.Equals(group.Mode, "sequential", StringComparison.Ordinal))
        {
            await SendSequentialAsync(group, result, data, principal, qMs, mMs, ct).ConfigureAwait(false);
        }
        else
        {
            await SendParallelAsync(group, result, data, principal, qMs, mMs, ct).ConfigureAwait(false);
        }

        ApplySuccessfulDivergence(result, group.DivergenceThreshold);
        return result;
    }

    public async Task<Result> SendAuthorizedAsync(
        string groupId,
        string data,
        string principal,
        IReadOnlyCollection<string> allowedWorkerIds,
        IReadOnlyCollection<string> refusedWorkerIds,
        int quiesceMs = 0,
        int maxResponseMs = 0,
        CancellationToken ct = default)
    {
        var group = AuthorizedGroup(groupId, principal);
        if (group is null) return await SendAsync(groupId, data, principal, quiesceMs, maxResponseMs, ct).ConfigureAwait(false);
        var dispatchGroup = new Group
        {
            GroupId = group.GroupId,
            Name = group.Name,
            WorkerIds = allowedWorkerIds.ToList(),
            CreatedBy = group.CreatedBy,
            CreatedAt = group.CreatedAt,
            Mode = group.Mode,
            StopOnFirstError = group.StopOnFirstError,
            ErrorPattern = group.ErrorPattern,
            QuiesceMs = group.QuiesceMs,
            MaxResponseMs = group.MaxResponseMs,
            DivergenceThreshold = group.DivergenceThreshold,
            Grants = group.Grants.ToList(),
        };
        var result = await SendGroupAsync(dispatchGroup, data, principal, quiesceMs, maxResponseMs, ct).ConfigureAwait(false);
        AddFailures(result, refusedWorkerIds);
        return result;
    }

    private Result EmptyResult(string groupId, string data) => new()
    {
        GroupId = groupId,
        SendId = _newId(),
        Command = data,
        SentAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
    };

    private async Task SendParallelAsync(
        Group group, Result result, string data, string principal, int quiesceMs, int maxResponseMs, CancellationToken ct)
    {
        var subscriptions = group.WorkerIds.Select(wid => _hub!.SubscribeOutput(wid)).ToArray();
        try
        {
            var sends = group.WorkerIds.Select(async wid =>
            {
                try
                {
                    await NotifyAsync(group, result, wid, data, principal, ct).ConfigureAwait(false);
                    return await _hub!.SendWorkerAsync(wid, InputFrame(data, result.SentAt), ct).ConfigureAwait(false);
                }
                catch when (!ct.IsCancellationRequested)
                {
                    return false;
                }
            }).ToArray();
            var accepted = await Task.WhenAll(sends).ConfigureAwait(false);
            var collects = group.WorkerIds.Select((wid, index) =>
                accepted[index]
                    ? CollectRowAsync(wid, subscriptions[index], quiesceMs, maxResponseMs, ct)
                    : Task.FromResult(new SessionResult { WorkerId = wid, Ok = false })).ToArray();
            result.Results.AddRange(await Task.WhenAll(collects).ConfigureAwait(false));
            result.FailedSessions.AddRange(result.Results.Where(row => !row.Ok).Select(row => row.WorkerId));
        }
        finally
        {
            foreach (var subscription in subscriptions)
            {
                await subscription.DisposeAsync().ConfigureAwait(false);
            }
        }
    }

    private async Task SendSequentialAsync(
        Group group, Result result, string data, string principal, int quiesceMs, int maxResponseMs, CancellationToken ct)
    {
        var stopped = false;
        Regex? errorPattern = string.IsNullOrEmpty(group.ErrorPattern)
            ? null
            : new Regex(group.ErrorPattern, RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1));
        foreach (var wid in group.WorkerIds)
        {
            if (stopped)
            {
                AddFailures(result, [wid]);
                continue;
            }

            await using var subscription = _hub!.SubscribeOutput(wid);
            var ok = false;
            try
            {
                await NotifyAsync(group, result, wid, data, principal, ct).ConfigureAwait(false);
                ok = await _hub.SendWorkerAsync(wid, InputFrame(data, result.SentAt), ct).ConfigureAwait(false);
            }
            catch when (!ct.IsCancellationRequested)
            {
                ok = false;
            }

            if (!ok)
            {
                AddFailures(result, [wid]);
                continue;
            }

            var row = await CollectRowAsync(wid, subscription, quiesceMs, maxResponseMs, ct).ConfigureAwait(false);
            result.Results.Add(row);
            if (group.StopOnFirstError && errorPattern is not null && errorPattern.IsMatch(row.OutputDelta ?? ""))
            {
                stopped = true;
            }
        }
    }

    private async Task<SessionResult> CollectRowAsync(
        string workerId, IFanoutOutputSubscription subscription, int quiesceMs, int maxResponseMs, CancellationToken ct)
    {
        try
        {
            var (output, elapsed) = await OutputCollector.CollectAsync(subscription, quiesceMs, maxResponseMs, ct)
                .ConfigureAwait(false);
            return new SessionResult { WorkerId = workerId, Ok = true, OutputDelta = output, ElapsedMs = elapsed };
        }
        catch when (!ct.IsCancellationRequested)
        {
            return new SessionResult { WorkerId = workerId, Ok = false };
        }
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
}
