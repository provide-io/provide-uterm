//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Hub;

/// <summary>
/// Clock abstraction matching Python time.monotonic / time.time plus cancellable sleep.
/// </summary>
public interface IClock
{
    double Monotonic();
    double Wall();
    Task SleepAsync(double seconds, CancellationToken cancellationToken = default);
}

/// <summary>Production clock backed by wall + process-relative monotonic time.</summary>
public sealed class RealClock : IClock
{
    private readonly long _baseTicks = Environment.TickCount64;

    public double Monotonic() => (Environment.TickCount64 - _baseTicks) / 1000.0;

    public double Wall() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public async Task SleepAsync(double seconds, CancellationToken cancellationToken = default)
    {
        if (seconds <= 0)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return;
        }

        await Task.Delay(TimeSpan.FromSeconds(seconds), cancellationToken).ConfigureAwait(false);
    }
}

/// <summary>Deterministic clock for tests — Sleep advances monotonic by <see cref="Step"/>.</summary>
public sealed class ManualClock : IClock
{
    private readonly object _gate = new();
    private double _mono;
    private double _wall;
    private double _step = 1.0;
    private readonly List<double> _sleeps = new();

    public ManualClock(double wall = 0)
    {
        _wall = wall;
    }

    public double Step
    {
        get { lock (_gate) return _step; }
        set { lock (_gate) _step = value; }
    }

    public void SetMonotonic(double v)
    {
        lock (_gate) _mono = v;
    }

    public void SetWall(double v)
    {
        lock (_gate) _wall = v;
    }

    public double Monotonic()
    {
        lock (_gate) return _mono;
    }

    public double Wall()
    {
        lock (_gate) return _wall;
    }

    public Task SleepAsync(double seconds, CancellationToken cancellationToken = default)
    {
        lock (_gate)
        {
            _sleeps.Add(seconds);
            _mono += _step;
        }

        return Task.CompletedTask;
    }

    public IReadOnlyList<double> Sleeps()
    {
        lock (_gate) return _sleeps.ToArray();
    }
}

internal static class ClockUtil
{
    public static IClock OrDefault(IClock? clock) => clock ?? new RealClock();
}
