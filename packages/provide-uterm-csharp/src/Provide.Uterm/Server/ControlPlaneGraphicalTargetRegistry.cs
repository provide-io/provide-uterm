//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.ControlPlane;

namespace Provide.Uterm.Server;

/// <summary>
/// A registry whose runtime targets live in the control plane, so they survive
/// a restart. Tenant-scope semantics are identical to
/// <see cref="InMemoryGraphicalTargetRegistry"/> — only the runtime storage
/// differs.
///
/// Static targets stay in memory. They are re-seeded from the config file on
/// every boot and are immutable at the API boundary, so persisting them would
/// create a second source of truth that could drift from the config.
/// </summary>
public sealed class ControlPlaneGraphicalTargetRegistry : IGraphicalTargetRegistry
{
    private readonly IEngine _engine;

    // Guards the static overlay and serializes operations, mirroring the
    // in-memory registry. The control plane has its own transaction-level
    // concurrency control; this keeps the overlay and the store consistent with
    // each other.
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly Dictionary<string, GraphicalTargetDefinition> _static = new(StringComparer.Ordinal);
    private bool _closed;

    public ControlPlaneGraphicalTargetRegistry(IEngine engine) => _engine = engine;

    /// <summary>Timestamp source (injected for deterministic tests).</summary>
    public Func<DateTimeOffset> Now { get; set; } = () => DateTimeOffset.UtcNow;

    /// <summary>Marks the registry closed; every later operation fails. Does NOT
    /// close the engine — its lifetime belongs to the caller.</summary>
    public void Close() => _closed = true;

    private void EnsureOpen(GraphicalTargetScope scope)
    {
        if (_closed)
        {
            throw new GraphicalTargetException(
                GraphicalTargetErrorCode.Closed, "graphical target registry is closed");
        }

        if (!scope.IsValid)
        {
            throw new GraphicalTargetException(
                GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
        }
    }

    public async Task<GraphicalTargetDefinition?> GetAsync(
        GraphicalTargetScope scope, string targetId, CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            EnsureOpen(scope);
            if (_static.TryGetValue(targetId, out var seeded) && scope.Permits(seeded.TenantId))
            {
                return seeded.Clone();
            }

            var rec = await _engine.GraphicalTargets().GetAsync(targetId, ct).ConfigureAwait(false);
            if (rec is null)
            {
                return null;
            }

            var def = ToDefinition(rec);
            return scope.Permits(def.TenantId) ? def : null;
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>Runtime + static merged (static wins on id collision),
    /// tenant-filtered, ordered by target_id.</summary>
    public async Task<IReadOnlyList<GraphicalTargetDefinition>> ListAsync(
        GraphicalTargetScope scope, CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            EnsureOpen(scope);
            var merged = new Dictionary<string, GraphicalTargetDefinition>(StringComparer.Ordinal);
            foreach (var rec in await _engine.GraphicalTargets().ListAsync(ct).ConfigureAwait(false))
            {
                var def = ToDefinition(rec);
                if (scope.Permits(def.TenantId))
                {
                    merged[def.TargetId] = def;
                }
            }

            foreach (var pair in _static)
            {
                if (scope.Permits(pair.Value.TenantId))
                {
                    merged[pair.Key] = pair.Value.Clone();
                }
            }

            return merged.Values
                .OrderBy(t => t.TargetId, StringComparer.Ordinal)
                .Select(t => t.Clone())
                .ToList();
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<GraphicalTargetDefinition> CreateAsync(
        GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            EnsureOpen(scope);
            var clone = target.Clone();
            if (!scope.Permits(clone.TenantId))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            clone.Validate();
            if (_static.ContainsKey(clone.TargetId))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.AlreadyExists, "graphical target already exists");
            }

            var store = _engine.GraphicalTargets();
            if (await store.GetAsync(clone.TargetId, ct).ConfigureAwait(false) is not null)
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.AlreadyExists, "graphical target already exists");
            }

            clone.CreatedAt = Now();
            await store.PutAsync(ToRecord(clone), ct).ConfigureAwait(false);
            return clone.Clone();
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task<GraphicalTargetDefinition> UpdateAsync(
        GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            EnsureOpen(scope);
            var clone = target.Clone();
            if (!scope.Permits(clone.TenantId))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            clone.Validate();
            if (_static.ContainsKey(clone.TargetId))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.Immutable, "static graphical target is immutable");
            }

            var store = _engine.GraphicalTargets();
            var existing = await store.GetAsync(clone.TargetId, ct).ConfigureAwait(false)
                ?? throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.NotFound, "graphical target not found");

            var current = ToDefinition(existing);
            if (!scope.Permits(current.TenantId))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            clone.CreatedAt = current.CreatedAt;
            clone.CreatedBy = current.CreatedBy;
            clone.UpdatedAt = Now();
            await store.PutAsync(ToRecord(clone), ct).ConfigureAwait(false);
            return clone.Clone();
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task DeleteAsync(
        GraphicalTargetScope scope, string targetId, CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            EnsureOpen(scope);
            if (_static.TryGetValue(targetId, out var seeded))
            {
                throw scope.Permits(seeded.TenantId)
                    ? new GraphicalTargetException(
                        GraphicalTargetErrorCode.Immutable, "static graphical target is immutable")
                    : new GraphicalTargetException(
                        GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            var store = _engine.GraphicalTargets();
            var existing = await store.GetAsync(targetId, ct).ConfigureAwait(false)
                ?? throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.NotFound, "graphical target not found");

            if (!scope.Permits(ToDefinition(existing).TenantId))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            await store.DeleteAsync(targetId, ct).ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>Seeds an immutable system target. Static targets are not
    /// persisted (see the type comment), so this only touches the overlay.</summary>
    public async Task AddStaticAsync(GraphicalTargetDefinition target, CancellationToken ct = default)
    {
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var clone = target.Clone();
            clone.Validate();
            clone.IsSystem = true;
            if (!_static.TryAdd(clone.TargetId, clone))
            {
                throw new GraphicalTargetException(
                    GraphicalTargetErrorCode.Conflict, "duplicate graphical target_id");
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    // Conversions. Times become epoch seconds to match every other cp_* table;
    // Config becomes JSON text so the same logical config always produces the
    // same bytes.
    private static GraphicalTargetRecord ToRecord(GraphicalTargetDefinition d) => new()
    {
        TargetId = d.TargetId,
        TenantId = d.TenantId ?? "",
        DisplayName = d.DisplayName,
        Protocol = d.Protocol,
        Endpoint = d.Endpoint,
        Secret = d.Secret,
        Width = d.Width,
        Height = d.Height,
        IsSystem = d.IsSystem,
        IsStatic = d.IsStatic,
        CaSecretRef = d.CaSecretRef,
        ClientCertSecretRef = d.ClientCertSecretRef,
        ClientKeySecretRef = d.ClientKeySecretRef,
        Config = d.Config.Count == 0 ? "{}" : JsonSerializer.Serialize(d.Config),
        CreatedBy = d.CreatedBy,
        CreatedAt = ToEpoch(d.CreatedAt),
        UpdatedBy = d.UpdatedBy,
        UpdatedAt = d.UpdatedAt is null ? null : ToEpoch(d.UpdatedAt.Value),
    };

    /// <summary>
    /// Rebuilds a definition. A config blob that fails to decode degrades to an
    /// empty object rather than failing the read: the column is
    /// non-authoritative protocol metadata, and refusing to list every target
    /// because one row is malformed turns a cosmetic defect into an outage.
    /// </summary>
    private static GraphicalTargetDefinition ToDefinition(GraphicalTargetRecord rec)
    {
        Dictionary<string, object?> config = [];
        if (!string.IsNullOrEmpty(rec.Config))
        {
            try
            {
                config = JsonSerializer.Deserialize<Dictionary<string, object?>>(rec.Config) ?? [];
            }
            catch (JsonException)
            {
                config = [];
            }
        }

        return new GraphicalTargetDefinition
        {
            TargetId = rec.TargetId,
            TenantId = rec.TenantId,
            DisplayName = rec.DisplayName,
            Protocol = rec.Protocol,
            Endpoint = rec.Endpoint,
            Secret = rec.Secret,
            Width = (int)rec.Width,
            Height = (int)rec.Height,
            IsSystem = rec.IsSystem,
            IsStatic = rec.IsStatic,
            CaSecretRef = rec.CaSecretRef,
            ClientCertSecretRef = rec.ClientCertSecretRef,
            ClientKeySecretRef = rec.ClientKeySecretRef,
            Config = config,
            CreatedBy = rec.CreatedBy,
            CreatedAt = FromEpoch(rec.CreatedAt),
            UpdatedBy = rec.UpdatedBy,
            UpdatedAt = rec.UpdatedAt is null ? null : FromEpoch(rec.UpdatedAt.Value),
        };
    }

    private static double ToEpoch(DateTimeOffset t) =>
        t == default ? 0 : t.ToUnixTimeMilliseconds() / 1000.0;

    private static DateTimeOffset FromEpoch(double v) =>
        v == 0 ? default : DateTimeOffset.FromUnixTimeMilliseconds((long)(v * 1000));
}
