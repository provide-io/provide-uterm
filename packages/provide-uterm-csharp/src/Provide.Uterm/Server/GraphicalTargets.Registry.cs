// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

namespace Provide.Uterm.Server;

public sealed class InMemoryGraphicalTargetRegistry : IGraphicalTargetRegistry
{
    private readonly object _gate = new();
    private readonly Dictionary<string, GraphicalTargetDefinition> _static = new(StringComparer.Ordinal);
    private readonly Dictionary<string, GraphicalTargetDefinition> _runtime = new(StringComparer.Ordinal);
    private bool _closed;

    public InMemoryGraphicalTargetRegistry(IEnumerable<GraphicalTargetDefinition>? staticTargets = null)
    {
        if (staticTargets is null)
        {
            return;
        }

        foreach (var target in staticTargets)
        {
            var clone = target.Clone();
            clone.IsSystem = true;
            clone.Validate();
            if (_static.ContainsKey(clone.TargetId))
            {
                throw new InvalidOperationException("duplicate graphical target_id");
            }

            _static[clone.TargetId] = clone;
        }
    }

    /// <summary>
    /// Mark the registry closed; every subsequent scoped operation refuses with
    /// <see cref="GraphicalTargetErrorCode.Closed"/>.
    ///
    /// The port carried the <c>_closed</c> field and the
    /// <see cref="EnsureOpen"/> guard from the start but never the method that
    /// sets it, so <see cref="GraphicalTargetErrorCode.Closed"/> was
    /// unreachable here while the reference
    /// (<c>provide/uterm/server/graphical_targets.py</c>) and the TypeScript
    /// port (<c>src/graphical/targets.ts</c>) both expose <c>close()</c>.
    ///
    /// Seeding through <see cref="AddStaticAsync"/> deliberately stays open
    /// after a close, matching both of those implementations.
    /// </summary>
    public void Close()
    {
        lock (_gate)
        {
            _closed = true;
        }
    }

    // The in-memory registry has no I/O, so each wrapper simply completes. The
    // async surface exists for the control-plane-backed implementation.
    public Task<GraphicalTargetDefinition?> GetAsync(
        GraphicalTargetScope scope, string targetId, CancellationToken ct = default) =>
        Task.FromResult(GetCore(scope, targetId));

    public Task<IReadOnlyList<GraphicalTargetDefinition>> ListAsync(
        GraphicalTargetScope scope, CancellationToken ct = default) =>
        Task.FromResult(ListCore(scope));

    public Task<GraphicalTargetDefinition> CreateAsync(
        GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default) =>
        Task.FromResult(CreateCore(scope, target));

    public Task<GraphicalTargetDefinition> UpdateAsync(
        GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default) =>
        Task.FromResult(UpdateCore(scope, target));

    public Task DeleteAsync(GraphicalTargetScope scope, string targetId, CancellationToken ct = default)
    {
        DeleteCore(scope, targetId);
        return Task.CompletedTask;
    }

    public Task AddStaticAsync(GraphicalTargetDefinition target, CancellationToken ct = default)
    {
        AddStaticCore(target);
        return Task.CompletedTask;
    }

    private GraphicalTargetDefinition? GetCore(GraphicalTargetScope scope, string targetId)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            if (_static.TryGetValue(targetId, out var staticTarget) && scope.Permits(staticTarget.TenantId))
            {
                return staticTarget.Clone();
            }

            if (_runtime.TryGetValue(targetId, out var runtimeTarget) && scope.Permits(runtimeTarget.TenantId))
            {
                return runtimeTarget.Clone();
            }

            return null;
        }
    }

    private IReadOnlyList<GraphicalTargetDefinition> ListCore(GraphicalTargetScope scope)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            var merged = new Dictionary<string, GraphicalTargetDefinition>(StringComparer.Ordinal);
            foreach (var pair in _runtime)
            {
                if (!scope.Permits(pair.Value.TenantId))
                {
                    continue;
                }

                merged[pair.Key] = pair.Value.Clone();
            }

            foreach (var pair in _static)
            {
                if (!scope.Permits(pair.Value.TenantId))
                {
                    continue;
                }

                merged[pair.Key] = pair.Value.Clone();
            }

            var outList = merged.Values.OrderBy(t => t.TargetId).Select(t => t.Clone()).ToList();
            return outList;
        }
    }

    private GraphicalTargetDefinition CreateCore(GraphicalTargetScope scope, GraphicalTargetDefinition target)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            var clone = target.Clone();
            if (!scope.Permits(clone.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            try
            {
                clone.Validate();
            }
            catch (ArgumentException ex)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, ex.Message);
            }

            if (_static.ContainsKey(clone.TargetId) || _runtime.ContainsKey(clone.TargetId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.AlreadyExists, "graphical target already exists");
            }

            clone.CreatedAt = DateTimeOffset.UtcNow;
            _runtime[clone.TargetId] = clone;
            return clone.Clone();
        }
    }

    private GraphicalTargetDefinition UpdateCore(GraphicalTargetScope scope, GraphicalTargetDefinition target)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            var clone = target.Clone();
            if (!scope.Permits(clone.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            try
            {
                clone.Validate();
            }
            catch (ArgumentException ex)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, ex.Message);
            }

            if (_static.ContainsKey(clone.TargetId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Immutable, "static graphical target is immutable");
            }

            if (!_runtime.TryGetValue(clone.TargetId, out var current))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.NotFound, "graphical target not found");
            }

            if (!scope.Permits(current.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            clone.CreatedAt = current.CreatedAt;
            clone.CreatedBy = current.CreatedBy;
            clone.UpdatedAt = DateTimeOffset.UtcNow;
            _runtime[clone.TargetId] = clone;
            return clone.Clone();
        }
    }

    private void DeleteCore(GraphicalTargetScope scope, string targetId)
    {
        lock (_gate)
        {
            EnsureOpen(scope);
            if (_static.TryGetValue(targetId, out var staticTarget))
            {
                if (!scope.Permits(staticTarget.TenantId))
                {
                    throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
                }

                throw new GraphicalTargetException(GraphicalTargetErrorCode.Immutable, "static graphical target is immutable");
            }

            if (!_runtime.TryGetValue(targetId, out var current))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.NotFound, "graphical target not found");
            }

            if (!scope.Permits(current.TenantId))
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
            }

            _runtime.Remove(targetId);
        }
    }

    private void AddStaticCore(GraphicalTargetDefinition target)
    {
        lock (_gate)
        {
            var clone = target.Clone();
            // Wrapped exactly as CreateCore and UpdateCore wrap it. Seeding used
            // to let a raw ArgumentException escape, so a bad target_id, protocol
            // or size arrived at a caller as an unhandled exception rather than
            // the INVALID refusal every other path produces.
            try
            {
                clone.Validate();
            }
            catch (ArgumentException ex)
            {
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Invalid, ex.Message);
            }

            clone.IsSystem = true;
            if (_static.ContainsKey(clone.TargetId))
            {
                // CONFLICT, not InvalidOperationException: the reference
                // (graphical_targets.py:433) and Go both raise a coded refusal
                // here, and the shared golden corpus records CONFLICT for
                // "seeding the same identifier twice". C# was the only port
                // throwing an uncoded exception, which reaches a REST caller as
                // a 500 instead of the refusal the other three give.
                throw new GraphicalTargetException(GraphicalTargetErrorCode.Conflict, "duplicate graphical target_id");
            }

            _static[clone.TargetId] = clone;
        }
    }

    private void EnsureOpen(GraphicalTargetScope scope)
    {
        if (_closed)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Closed, "graphical target registry is closed");
        }

        if (!scope.IsValid)
        {
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Forbidden, "graphical target tenant scope denied");
        }
    }
}
