//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Server;
using Def = Provide.Uterm.Server.GraphicalTargetDefinition;

namespace Provide.Uterm.Tests.Gui;

/// <summary>
/// The closed-registry contract, mirroring the shared golden scenario
/// "a closed registry does nothing at all"
/// (packages/provide-uterm-ts/testdata/graphicaltargets_golden.json), which the
/// reference and the TypeScript port both execute.
///
/// The port carried the closed-state guard without the method that sets it, so
/// none of this was reachable in C# and no test could have noticed: the
/// compiler's CS0649 on the never-assigned field was the only signal.
/// Runs in the ~Gui gate batch.
/// </summary>
public class GraphicalTargetRegistryCloseTests
{
    private static Def Target(string targetId) =>
        new() { TargetId = targetId, Protocol = "memory", Width = 10, Height = 10 };

    private static async Task AssertClosedAsync(Func<Task> operation)
    {
        var ex = await Assert.ThrowsAsync<GraphicalTargetException>(operation);
        Assert.Equal(GraphicalTargetErrorCode.Closed, ex.Code);
        Assert.Equal("graphical target registry is closed", ex.Message);
    }

    [Fact]
    public async Task ClosedRegistry_RefusesEveryScopedOperation()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        var sys = GraphicalTargetScope.System();
        await reg.CreateAsync(sys, Target("vm1"));

        reg.Close();

        await AssertClosedAsync(async () => await reg.GetAsync(sys, "vm1"));
        await AssertClosedAsync(async () => await reg.ListAsync(sys));
        await AssertClosedAsync(async () => await reg.CreateAsync(sys, Target("vm2")));
        await AssertClosedAsync(async () => await reg.UpdateAsync(sys, Target("vm1")));
        await AssertClosedAsync(async () => await reg.DeleteAsync(sys, "vm1"));
    }

    [Fact]
    public async Task ClosedRegistry_StillSeeds()
    {
        // Seeding is neither scope-gated nor closed-gated on the reference, so
        // it must keep working here too: a closed registry that refused its own
        // seeding would diverge on the golden's last step.
        var reg = new InMemoryGraphicalTargetRegistry();
        reg.Close();

        await reg.AddStaticAsync(Target("seed"));

        // The seed really landed: a duplicate identifier is what proves it,
        // since every read path refuses once the registry is closed. CONFLICT
        // rather than InvalidOperationException, matching the reference and the
        // shared golden corpus.
        var duplicate = await Assert.ThrowsAsync<GraphicalTargetException>(
            async () => await reg.AddStaticAsync(Target("seed")));
        Assert.Equal(GraphicalTargetErrorCode.Conflict, duplicate.Code);
    }

    [Fact]
    public async Task Close_IsIdempotent()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        var sys = GraphicalTargetScope.System();

        reg.Close();
        reg.Close();

        await AssertClosedAsync(async () => await reg.ListAsync(sys));
    }

    [Fact]
    public async Task OpenRegistry_IsUnaffected()
    {
        // The guard must not fire before Close: an always-closed registry would
        // pass every assertion above and still be wrong.
        var reg = new InMemoryGraphicalTargetRegistry();
        var sys = GraphicalTargetScope.System();

        await reg.CreateAsync(sys, Target("vm1"));

        Assert.NotNull(await reg.GetAsync(sys, "vm1"));
        Assert.Single(await reg.ListAsync(sys));
    }
}
