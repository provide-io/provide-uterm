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
