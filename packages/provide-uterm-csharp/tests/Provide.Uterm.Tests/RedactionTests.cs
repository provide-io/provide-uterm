//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using R = Provide.Uterm.Redaction.Redaction;

namespace Provide.Uterm.Tests;

public class RedactionTests
{
    [Fact]
    public void MakeRedactor_EmptyIsIdentity()
    {
        var r = R.MakeRedactor(Array.Empty<string>());
        Assert.Equal("secret", r("secret"));
    }

    [Fact]
    public void MakeRedactor_ReplacesMatches()
    {
        var r = R.MakeRedactor([@"password=\S+"]);
        // Full-match replace: the entire pattern match becomes [REDACTED].
        Assert.Equal("user [REDACTED] ok", r("user password=hunter2 ok"));
    }

    [Fact]
    public void RedactText_NullRedactorIsIdentity()
    {
        Assert.Equal("x", R.RedactText("x", null));
    }

    [Fact]
    public void MakeRedactor_InvalidPatternThrows()
    {
        Assert.ThrowsAny<Exception>(() => R.MakeRedactor(["["]));
    }
}
