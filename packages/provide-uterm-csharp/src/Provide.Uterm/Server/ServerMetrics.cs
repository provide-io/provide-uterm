//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;

namespace Provide.Uterm.Server;

/// <summary>
/// Simple thread-safe counter for internal server metrics, maintaining parity
/// with the Python implementation's internal metrics state while also emitting
/// to the OpenTelemetry pipeline.
/// </summary>
public sealed class ServerMetrics
{
    private readonly ConcurrentDictionary<string, long> _counters = new();

    public void Increment(string name, long value)
    {
        _counters.AddOrUpdate(name, value, (_, prev) => prev + value);
        Provide.Telemetry.Metrics.Counter(name).Add(value);
    }

    public void Increment(string name, int value) => Increment(name, (long)value);

    public IReadOnlyDictionary<string, long> GetSnapshot() => _counters;
}
