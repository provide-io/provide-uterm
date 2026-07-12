//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Net;
using System.Text;
using System.Text.Json;

namespace Provide.Uterm.Manager;

public sealed class ManagerConfig
{
    public string Host { get; set; } = "127.0.0.1";
    public int Port { get; set; } = 8790;
    public string? AuthToken { get; set; }
    public string LogDir { get; set; } = ".uterm-manager-logs";
    public List<string> CorsOrigins { get; set; } = new() { "http://127.0.0.1:8790" };
}

public sealed class AgentRecord
{
    public required string AgentId { get; set; }
    public string WorkerType { get; set; } = "default";
    public string State { get; set; } = "idle";
    public double CreatedAt { get; set; } = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
    public Dictionary<string, object?> Meta { get; set; } = new();
}

/// <summary>In-memory agent fleet manager.</summary>
public sealed class AgentManager
{
    private readonly ConcurrentDictionary<string, AgentRecord> _agents = new();
    private readonly ConcurrentQueue<Dictionary<string, object?>> _timeseries = new();
    private readonly ManagerConfig _cfg;

    public AgentManager(ManagerConfig? cfg = null) => _cfg = cfg ?? new ManagerConfig();

    public ManagerConfig Config => _cfg;

    public Dictionary<string, object?> GetSwarmStatus() => new()
    {
        ["agents"] = _agents.Count,
        ["by_state"] = _agents.Values.GroupBy(a => a.State)
            .ToDictionary(g => g.Key, g => (object)g.Count()),
        ["agent_ids"] = _agents.Keys.OrderBy(k => k, StringComparer.Ordinal).ToList(),
    };

    public AgentRecord Spawn(string workerType = "default", string? agentId = null)
    {
        var id = string.IsNullOrEmpty(agentId) ? "agent-" + Guid.NewGuid().ToString("N")[..10] : agentId;
        var rec = new AgentRecord { AgentId = id, WorkerType = workerType, State = "running" };
        _agents[id] = rec;
        _timeseries.Enqueue(new Dictionary<string, object?>
        {
            ["ts"] = rec.CreatedAt,
            ["event"] = "spawn",
            ["agent_id"] = id,
            ["worker_type"] = workerType,
        });
        return rec;
    }

    public bool Stop(string agentId)
    {
        if (!_agents.TryGetValue(agentId, out var rec)) return false;
        rec.State = "stopped";
        _timeseries.Enqueue(new Dictionary<string, object?>
        {
            ["ts"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            ["event"] = "stop",
            ["agent_id"] = agentId,
        });
        return true;
    }

    public bool Remove(string agentId) => _agents.TryRemove(agentId, out _);

    public AgentRecord? Get(string agentId) => _agents.TryGetValue(agentId, out var r) ? r : null;

    public IReadOnlyList<AgentRecord> List() => _agents.Values.OrderBy(a => a.AgentId, StringComparer.Ordinal).ToList();

    public Dictionary<string, object?> GetTimeseriesInfo() => new()
    {
        ["rows"] = _timeseries.Count,
        ["plugin"] = "memory",
    };

    public IReadOnlyList<Dictionary<string, object?>> GetTimeseriesRecent(int limit = 200)
    {
        var all = _timeseries.ToArray();
        if (limit <= 0) limit = 200;
        return all.TakeLast(limit).ToList();
    }

    public Dictionary<string, object?> GetTimeseriesSummary(int windowMinutes = 120) => new()
    {
        ["window_minutes"] = windowMinutes,
        ["events"] = _timeseries.Count,
        ["agents"] = _agents.Count,
    };
}

/// <summary>HTTP surface for the agent manager (health + swarm routes).</summary>
public sealed class ManagerServer : IAsyncDisposable
{
    private readonly AgentManager _manager;
    private HttpListener? _listener;
    private CancellationTokenSource? _cts;
    private Task? _loop;

    public ManagerServer(AgentManager manager) => _manager = manager;

    public AgentManager Manager => _manager;
    public string? BaseAddress { get; private set; }

    public async Task StartAsync(CancellationToken cancellationToken = default)
    {
        var cfg = _manager.Config;
        var prefix = $"http://{cfg.Host}:{cfg.Port}/";
        _listener = new HttpListener();
        _listener.Prefixes.Add(prefix);
        _listener.Start();
        BaseAddress = prefix.TrimEnd('/');
        _cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        _loop = Task.Run(() => AcceptLoopAsync(_cts.Token));
        await Task.CompletedTask.ConfigureAwait(false);
    }

    public async Task StopAsync()
    {
        _cts?.Cancel();
        _listener?.Stop();
        if (_loop is not null)
        {
            try { await _loop.ConfigureAwait(false); }
            catch { /* cancelled */ }
        }
    }

    public async ValueTask DisposeAsync() => await StopAsync().ConfigureAwait(false);

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _listener is not null)
        {
            HttpListenerContext ctx;
            try
            {
                ctx = await _listener.GetContextAsync().WaitAsync(ct).ConfigureAwait(false);
            }
            catch
            {
                break;
            }

            _ = Task.Run(() => HandleAsync(ctx), CancellationToken.None);
        }
    }

    private async Task HandleAsync(HttpListenerContext ctx)
    {
        try
        {
            if (!Authorize(ctx))
            {
                await WriteJson(ctx, 401, new { detail = "unauthorized" }).ConfigureAwait(false);
                return;
            }

            var path = ctx.Request.Url?.AbsolutePath.TrimEnd('/') ?? "";
            var method = ctx.Request.HttpMethod.ToUpperInvariant();

            if (method == "GET" && path is "/health" or "")
            {
                await WriteJson(ctx, 200, new { status = "ok" }).ConfigureAwait(false);
                return;
            }

            if (method == "GET" && path == "/swarm/status")
            {
                await WriteJson(ctx, 200, _manager.GetSwarmStatus()).ConfigureAwait(false);
                return;
            }

            if (method == "GET" && path == "/swarm/timeseries/info")
            {
                await WriteJson(ctx, 200, _manager.GetTimeseriesInfo()).ConfigureAwait(false);
                return;
            }

            if (method == "GET" && path == "/swarm/timeseries/recent")
            {
                var limit = 200;
                var q = ctx.Request.QueryString["limit"];
                if (int.TryParse(q, out var n)) limit = n;
                await WriteJson(ctx, 200, new
                {
                    rows = _manager.GetTimeseriesRecent(limit),
                    info = _manager.GetTimeseriesInfo(),
                }).ConfigureAwait(false);
                return;
            }

            if (method == "GET" && path == "/swarm/agents")
            {
                await WriteJson(ctx, 200, _manager.List()).ConfigureAwait(false);
                return;
            }

            if (method == "POST" && path == "/swarm/agents")
            {
                var body = await ReadBody(ctx).ConfigureAwait(false);
                var workerType = body.TryGetValue("worker_type", out var wt) ? Convert.ToString(wt) ?? "default" : "default";
                var rec = _manager.Spawn(workerType);
                await WriteJson(ctx, 200, rec).ConfigureAwait(false);
                return;
            }

            if (method == "POST" && path.StartsWith("/swarm/agents/", StringComparison.Ordinal) && path.EndsWith("/stop", StringComparison.Ordinal))
            {
                var id = path["/swarm/agents/".Length..^"/stop".Length];
                var ok = _manager.Stop(id);
                await WriteJson(ctx, ok ? 200 : 404, new { ok }).ConfigureAwait(false);
                return;
            }

            if (method == "DELETE" && path.StartsWith("/swarm/agents/", StringComparison.Ordinal))
            {
                var id = path["/swarm/agents/".Length..];
                var ok = _manager.Remove(id);
                await WriteJson(ctx, ok ? 200 : 404, new { ok }).ConfigureAwait(false);
                return;
            }

            await WriteJson(ctx, 404, new { detail = "not found" }).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            try { await WriteJson(ctx, 500, new { detail = ex.Message }).ConfigureAwait(false); }
            catch { /* ignore */ }
        }
    }

    private bool Authorize(HttpListenerContext ctx)
    {
        var token = _manager.Config.AuthToken;
        if (string.IsNullOrEmpty(token)) return true;
        var auth = ctx.Request.Headers["Authorization"] ?? "";
        return auth == "Bearer " + token;
    }

    private static async Task<Dictionary<string, object?>> ReadBody(HttpListenerContext ctx)
    {
        using var reader = new StreamReader(ctx.Request.InputStream, ctx.Request.ContentEncoding);
        var raw = await reader.ReadToEndAsync().ConfigureAwait(false);
        if (string.IsNullOrWhiteSpace(raw)) return new Dictionary<string, object?>();
        try
        {
            return JsonSerializer.Deserialize<Dictionary<string, object?>>(raw) ?? new Dictionary<string, object?>();
        }
        catch
        {
            return new Dictionary<string, object?>();
        }
    }

    private static async Task WriteJson(HttpListenerContext ctx, int status, object body)
    {
        var json = JsonSerializer.Serialize(body);
        var bytes = Encoding.UTF8.GetBytes(json);
        ctx.Response.StatusCode = status;
        ctx.Response.ContentType = "application/json";
        ctx.Response.ContentLength64 = bytes.Length;
        await ctx.Response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
        ctx.Response.OutputStream.Close();
    }
}

/// <summary>CLI entry helpers for uterm-manager.</summary>
public static class ManagerProgram
{
    public static async Task<int> RunAsync(string[] args)
    {
        if (args.Any(a => a is "-h" or "--help" or "help"))
        {
            Console.WriteLine("uterm-manager — provide-uterm agent fleet manager");
            Console.WriteLine();
            Console.WriteLine("Usage:");
            Console.WriteLine("  uterm-manager [--host HOST] [--port PORT] [--token TOKEN]");
            Console.WriteLine();
            Console.WriteLine("Routes:");
            Console.WriteLine("  GET  /health");
            Console.WriteLine("  GET  /swarm/status");
            Console.WriteLine("  GET  /swarm/agents");
            Console.WriteLine("  POST /swarm/agents");
            Console.WriteLine("  POST /swarm/agents/{id}/stop");
            Console.WriteLine("  DELETE /swarm/agents/{id}");
            Console.WriteLine("  GET  /swarm/timeseries/info");
            Console.WriteLine("  GET  /swarm/timeseries/recent");
            return 0;
        }

        var cfg = new ManagerConfig();
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i] == "--host" && i + 1 < args.Length) cfg.Host = args[++i];
            else if (args[i] == "--port" && i + 1 < args.Length && int.TryParse(args[++i], out var p)) cfg.Port = p;
            else if (args[i] == "--token" && i + 1 < args.Length) cfg.AuthToken = args[++i];
        }

        var manager = new AgentManager(cfg);
        await using var server = new ManagerServer(manager);
        await server.StartAsync().ConfigureAwait(false);
        Console.WriteLine($"uterm-manager listening on {server.BaseAddress}");
        using var cts = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            cts.Cancel();
        };
        try
        {
            await Task.Delay(Timeout.Infinite, cts.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            // shutdown
        }

        return 0;
    }
}
