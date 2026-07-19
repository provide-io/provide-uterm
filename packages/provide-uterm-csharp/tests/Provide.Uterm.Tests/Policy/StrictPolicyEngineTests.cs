//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Frames;
using Provide.Uterm.Policy;
using Xunit;

namespace Provide.Uterm.Tests.Policy;

public class StrictPolicyEngineTests
{
    private static JsonDocument LoadVectors()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "testdata", "behavior", "behavior_vectors.json");
        var json = File.ReadAllText(path);
        return JsonDocument.Parse(json);
    }

    [Fact]
    public void CanInject_ViewerDenied_OperatorAllowed()
    {
        var p = new StrictPolicyEngine();
        Assert.Equal(StrictPolicyEngine.ErrInsufficientRole, p.CanInject("s", "lease", "viewer"));
        Assert.Null(p.CanInject("s", "lease", "operator"));
        Assert.Equal(StrictPolicyEngine.ErrNoActiveLease, p.CanInject("s", "", "operator"));
    }

    [Fact]
    public void PolicyMatchesBehaviorVectors()
    {
        using var doc = LoadVectors();
        var p = new StrictPolicyEngine();
        foreach (var el in doc.RootElement.GetProperty("policy_cases").EnumerateArray())
        {
            var op = el.GetProperty("op").GetString()!;
            var role = el.GetProperty("role").GetString()!;
            var leaseOwned = el.GetProperty("lease_owned").GetBoolean();
            var sessionActive = el.GetProperty("session_active").GetBoolean();
            var allowed = el.GetProperty("allowed").GetBoolean();
            string? wantErr = el.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String
                ? e.GetString()
                : null;

            var err = p.CanPerform(op, role, leaseOwned, sessionActive);
            if (allowed)
            {
                Assert.True(err is null, $"expected allow for {op}/{role}: {err}");
            }
            else
            {
                Assert.Equal(wantErr, err);
            }
        }
    }

    [Fact]
    public void HelloDefaultsMatchContract()
    {
        using var doc = LoadVectors();
        var expected = doc.RootElement.GetProperty("hello_defaults").GetProperty("csharp");
        var hello = FrameBuilders.MakeHelloFrameWithDefaults();
        Assert.Equal(expected.GetProperty("mcp_supported").GetBoolean(), hello.McpSupported);
        Assert.Equal(expected.GetProperty("vnc_supported").GetBoolean(), hello.VncSupported);
    }

    [Theory]
    [InlineData("viewer", true, true, false)]
    [InlineData("operator", true, true, true)]
    [InlineData("admin", true, false, true)] // input_inject ignores session_active
    [InlineData("operator", false, true, false)]
    public void InputInject_ParamMatrix(string role, bool lease, bool session, bool expectAllow)
    {
        var err = new StrictPolicyEngine().CanPerform("input_inject", role, lease, session);
        Assert.Equal(expectAllow, err is null);
    }
}
