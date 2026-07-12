//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>Input-buffer + lifecycle/policy helper service.</summary>
public sealed class StateStore
{
    private readonly WorkerRegistry _registry;
    private readonly object _lock;
    private readonly IClock _clock;
    private readonly int _maxBufferChars;
    private readonly Action<string, int>? _onMetric;
    private readonly Action<string, bool, string?>? _onHijackChanged;
    private readonly object _buffersGate = new();
    private readonly Dictionary<object, string> _inputBuffers = new();

    public StateStore(
        WorkerRegistry registry,
        object sharedLock,
        IClock? clock = null,
        int maxBufferChars = 40000,
        Action<string, int>? onMetric = null,
        Action<string, bool, string?>? onHijackChanged = null)
    {
        _registry = registry;
        _lock = sharedLock;
        _clock = ClockUtil.OrDefault(clock);
        _maxBufferChars = maxBufferChars;
        _onMetric = onMetric;
        _onHijackChanged = onHijackChanged;
    }

    public static int ClampLease(int leaseS)
    {
        if (leaseS < 1) return 1;
        if (leaseS > 14400) return 14400;
        return leaseS;
    }

    public bool HasValidRestLease(WorkerTermState st) =>
        st.HijackSession is not null && st.HijackSession.LeaseExpiresAt > _clock.Monotonic();

    public bool IsDashboardHijackActive(WorkerTermState st)
    {
        if (st.HijackOwner is null) return false;
        if (st.HijackOwnerExpiresAt is null) return true;
        return st.HijackOwnerExpiresAt.Value > _clock.Monotonic();
    }

    public bool IsHijacked(WorkerTermState st) =>
        IsDashboardHijackActive(st) || HasValidRestLease(st);

    public void Metric(string name, int value = 1) => _onMetric?.Invoke(name, value);

    public void NotifyHijackChanged(string workerId, bool enabled, string? owner) =>
        _onHijackChanged?.Invoke(workerId, enabled, owner);

    public void TouchActivity(string workerId)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is not null)
            {
                st.LastActivityAt = _clock.Monotonic();
            }
        }
    }

    public WorkerTermState GetOrCreate(string workerId)
    {
        lock (_lock)
        {
            var st = _registry.Get(workerId);
            if (st is null)
            {
                st = new WorkerTermState();
                _registry.Put(workerId, st);
            }

            return st;
        }
    }

    public (string Command, bool Ok) BufferAndGetCommand(object ws, string data)
    {
        lock (_buffersGate)
        {
            var buf = (_inputBuffers.TryGetValue(ws, out var existing) ? existing : "") + data;
            if (buf.Length > _maxBufferChars)
            {
                _inputBuffers.Remove(ws);
                return ("", false);
            }

            if (buf.Contains('\r') || buf.Contains('\n'))
            {
                _inputBuffers.Remove(ws);
                return (buf, true);
            }

            _inputBuffers[ws] = buf;
            return ("", false);
        }
    }

    public int Shutdown() => 0;
}
