//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Server;

namespace Provide.Uterm.Tests.Server;

public sealed class WorkerBearerAuthenticationTests
{
    [Theory]
    [InlineData("Bearer secret", "secret", true)]
    [InlineData("Bearer wrong", "secret", false)]
    [InlineData("Bearer prefix-secret", "secret", false)]
    [InlineData("Bearer secret-suffix", "secret", false)]
    [InlineData("Basic secret", "secret", false)]
    [InlineData("bearer secret", "secret", false)]
    [InlineData("Bearer", "secret", false)]
    [InlineData("Bearer   secret  ", "secret", true)]
    [InlineData("Bearer sëcret", "sëcret", true)]
    [InlineData("Bearer sécret", "sëcret", false)]
    public void ValidatesReferenceBearerGrammar(string authorization, string expectedToken, bool expected)
    {
        Assert.Equal(expected, WorkerBearerAuthentication.IsAuthorized(authorization, expectedToken));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    public void MissingConfiguredTokenDisablesWorkerAuthentication(string? expectedToken)
    {
        Assert.True(WorkerBearerAuthentication.IsAuthorized("anything", expectedToken));
    }
}
