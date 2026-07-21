//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Server;

/// <summary>Simple counter metrics (Go server Metrics).</summary>
public sealed class ServerMetrics
{
    private readonly object _gate = new();
    private readonly Dictionary<string, long> _counters = new(StringComparer.Ordinal);

    public void Inc(string name, long delta = 1)
    {
        if (string.IsNullOrEmpty(name)) return;
        lock (_gate)
        {
            _counters.TryGetValue(name, out var v);
            _counters[name] = v + delta;
        }
    }

    public Dictionary<string, long> Snapshot()
    {
        lock (_gate)
        {
            return new Dictionary<string, long>(_counters);
        }
    }

    public string Prometheus()
    {
        var sb = new StringBuilder();
        lock (_gate)
        {
            foreach (var (k, v) in _counters.OrderBy(kv => kv.Key, StringComparer.Ordinal))
            {
                var name = k.Replace('-', '_').Replace('.', '_');
                sb.Append("# TYPE uterm_").Append(name).Append(" counter\n");
                sb.Append("uterm_").Append(name).Append(' ').Append(v).Append('\n');
            }
        }

        return sb.ToString();
    }
}
