//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Raised when a worker id is not registered.</summary>
public sealed class WorkerNotFoundException : Exception
{
    public string WorkerId { get; }

    public WorkerNotFoundException(string workerId)
        : base($"worker not found: \"{workerId}\"")
    {
        WorkerId = workerId;
    }
}

/// <summary>In-memory registry of attached workers keyed by worker id.</summary>
public sealed class WorkerRegistry
{
    private readonly object _gate = new();
    private readonly Dictionary<string, WorkerTermState> _workers = new();

    public WorkerTermState? Get(string workerId)
    {
        lock (_gate)
        {
            return _workers.TryGetValue(workerId, out var st) ? st : null;
        }
    }

    public WorkerTermState Require(string workerId)
    {
        lock (_gate)
        {
            if (!_workers.TryGetValue(workerId, out var st))
            {
                throw new WorkerNotFoundException(workerId);
            }

            return st;
        }
    }

    public void Put(string workerId, WorkerTermState state)
    {
        lock (_gate) _workers[workerId] = state;
    }

    public WorkerTermState SetDefault(string workerId, WorkerTermState state)
    {
        lock (_gate)
        {
            if (_workers.TryGetValue(workerId, out var existing))
            {
                return existing;
            }

            _workers[workerId] = state;
            return state;
        }
    }

    public WorkerTermState? Pop(string workerId)
    {
        lock (_gate)
        {
            if (!_workers.TryGetValue(workerId, out var st))
            {
                return null;
            }

            _workers.Remove(workerId);
            return st;
        }
    }

    public bool Discard(string workerId)
    {
        lock (_gate) return _workers.Remove(workerId);
    }

    public bool Contains(string workerId)
    {
        lock (_gate) return _workers.ContainsKey(workerId);
    }

    public IReadOnlyList<WorkerTermState> All()
    {
        lock (_gate) return _workers.Values.ToList();
    }

    public IReadOnlyList<string> Keys()
    {
        lock (_gate)
        {
            var keys = _workers.Keys.ToList();
            keys.Sort(StringComparer.Ordinal);
            return keys;
        }
    }

    public int Count
    {
        get { lock (_gate) return _workers.Count; }
    }
}
