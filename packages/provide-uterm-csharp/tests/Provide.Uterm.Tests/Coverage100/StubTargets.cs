//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>A target registry that returns one canned answer or throws.</summary>
internal sealed class StubTargets : IGraphicalTargetRegistry
{
    public Func<string, GraphicalTargetDefinition?> Get { get; set; } = _ => null;

    public Task<GraphicalTargetDefinition?> GetAsync(GraphicalTargetScope scope, string targetId, CancellationToken ct = default) =>
        Task.FromResult(Get(targetId));

    public Task<IReadOnlyList<GraphicalTargetDefinition>> ListAsync(GraphicalTargetScope scope, CancellationToken ct = default) =>
        throw new GraphicalTargetException(GraphicalTargetErrorCode.Closed, "closed");

    public Task<GraphicalTargetDefinition> CreateAsync(GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default) =>
        throw new NotSupportedException();

    public Task<GraphicalTargetDefinition> UpdateAsync(GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default) =>
        throw new NotSupportedException();

    public Task DeleteAsync(GraphicalTargetScope scope, string targetId, CancellationToken ct = default) =>
        throw new NotSupportedException();

    public Task AddStaticAsync(GraphicalTargetDefinition target, CancellationToken ct = default) =>
        throw new NotSupportedException();
}
