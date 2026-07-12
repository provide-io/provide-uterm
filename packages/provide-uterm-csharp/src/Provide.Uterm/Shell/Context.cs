//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Shell;

/// <summary>Cloudflare KV binding used by kv command. Port of Go shell.KVStore.</summary>
public interface IKvStore
{
    Task<IReadOnlyList<string>> ListAsync(string prefix, CancellationToken ct = default);
    Task<string?> GetAsync(string key, CancellationToken ct = default);
    Task PutAsync(string key, string value, CancellationToken ct = default);
    Task DeleteAsync(string key, CancellationToken ct = default);
}

/// <summary>Durable-Object namespace for sessions kill. Port of Go shell.DONamespace.</summary>
public interface IDoNamespace
{
    Task KillAsync(string sessionId, CancellationToken ct = default);
}

/// <summary>DO storage handle. Port of Go shell.Storage.</summary>
public interface IShellStorage
{
    Task<IReadOnlyList<string>> ListAsync(CancellationToken ct = default);
    Task<string?> GetAsync(string key, CancellationToken ct = default);
}

/// <summary>Cloudflare env object. Port of Go shell.Env.</summary>
public interface IShellEnv
{
    IKvStore? Registry();
    IDoNamespace? Runtime();
    IReadOnlyDictionary<string, string> Attrs();
}

/// <summary>Lists sessions from the KV registry. Port of Go shell.SessionLister.</summary>
public delegate Task<IReadOnlyList<IReadOnlyDictionary<string, object?>>> SessionLister(
    CancellationToken ct = default);

/// <summary>Runtime context for CommandDispatcher. Port of Go shell.Context.</summary>
public sealed class ShellContext
{
    public Dictionary<string, object?> Values { get; init; } = new();
    public IShellEnv? Env { get; init; }
    public IShellStorage? Storage { get; init; }
    public SessionLister? ListKvSessions { get; init; }
}

/// <summary>In-memory KV for tests and standalone ushell.</summary>
public sealed class MemoryKvStore : IKvStore
{
    private readonly Dictionary<string, string> _data = new(StringComparer.Ordinal);

    public Task<IReadOnlyList<string>> ListAsync(string prefix, CancellationToken ct = default)
    {
        var keys = _data.Keys.Where(k => k.StartsWith(prefix, StringComparison.Ordinal)).OrderBy(k => k, StringComparer.Ordinal).ToList();
        return Task.FromResult<IReadOnlyList<string>>(keys);
    }

    public Task<string?> GetAsync(string key, CancellationToken ct = default) =>
        Task.FromResult(_data.TryGetValue(key, out var v) ? v : null);

    public Task PutAsync(string key, string value, CancellationToken ct = default)
    {
        _data[key] = value;
        return Task.CompletedTask;
    }

    public Task DeleteAsync(string key, CancellationToken ct = default)
    {
        _data.Remove(key);
        return Task.CompletedTask;
    }
}

/// <summary>In-memory storage for tests.</summary>
public sealed class MemoryShellStorage : IShellStorage
{
    private readonly Dictionary<string, string> _data = new(StringComparer.Ordinal);

    public void Put(string key, string value) => _data[key] = value;

    public Task<IReadOnlyList<string>> ListAsync(CancellationToken ct = default) =>
        Task.FromResult<IReadOnlyList<string>>(_data.Keys.OrderBy(k => k, StringComparer.Ordinal).ToList());

    public Task<string?> GetAsync(string key, CancellationToken ct = default) =>
        Task.FromResult(_data.TryGetValue(key, out var v) ? v : null);
}

/// <summary>In-memory env binding for tests.</summary>
public sealed class MemoryShellEnv : IShellEnv
{
    private readonly IKvStore? _registry;
    private readonly IDoNamespace? _runtime;
    private readonly Dictionary<string, string> _attrs;

    public MemoryShellEnv(IKvStore? registry = null, IDoNamespace? runtime = null, IReadOnlyDictionary<string, string>? attrs = null)
    {
        _registry = registry;
        _runtime = runtime;
        _attrs = attrs is null
            ? new Dictionary<string, string>(StringComparer.Ordinal)
            : new Dictionary<string, string>(attrs, StringComparer.Ordinal);
    }

    public IKvStore? Registry() => _registry;
    public IDoNamespace? Runtime() => _runtime;
    public IReadOnlyDictionary<string, string> Attrs() => _attrs;
}

/// <summary>Recording DO namespace for tests.</summary>
public sealed class MemoryDoNamespace : IDoNamespace
{
    public List<string> Killed { get; } = new();

    public Task KillAsync(string sessionId, CancellationToken ct = default)
    {
        Killed.Add(sessionId);
        return Task.CompletedTask;
    }
}
