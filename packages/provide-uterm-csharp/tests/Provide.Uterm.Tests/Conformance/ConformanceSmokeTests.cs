//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Tests.Conformance;

/// <summary>
/// AC3 byte-exact Python differential proof lives in
/// <see cref="ConformanceVectorsTests"/> (port of Go conformance/conformance_test.go).
/// This file remains only as a pointer so the Conformance/ directory is not empty.
/// </summary>
public class ConformanceSmokeTests
{
    [Fact]
    public void DriverLivesIn_ConformanceVectorsTests()
    {
        Assert.True(typeof(ConformanceVectorsTests).IsClass);
    }
}
