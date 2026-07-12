//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.DeckMux;

namespace Provide.Uterm.Tests;

public class DeckMuxIdentityTests
{
    private static readonly string GoldenPath = Path.Combine(
        AppContext.BaseDirectory, "testdata", "deckmux", "python_golden.json");

    // Fall back to source tree path when not copied to output.
    private static string ResolveGolden()
    {
        if (File.Exists(GoldenPath))
        {
            return GoldenPath;
        }

        var alt = Path.GetFullPath(Path.Combine(
            AppContext.BaseDirectory, "..", "..", "..", "testdata", "deckmux", "python_golden.json"));
        return alt;
    }

    [Fact]
    public void GenerateName_Color_Initials_MatchPythonGolden()
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(ResolveGolden()));
        var root = doc.RootElement;

        foreach (var entry in root.GetProperty("names").EnumerateArray())
        {
            var id = entry.GetProperty("id").GetString()!;
            var expectedName = entry.GetProperty("name").GetString()!;
            var expectedColor = entry.GetProperty("color").GetString()!;
            var expectedInitials = entry.GetProperty("initials").GetString()!;

            Assert.Equal(expectedName, IdentityNames.GenerateName(id));
            Assert.Equal(expectedColor, IdentityNames.GenerateColor(id));
            Assert.Equal(expectedInitials, IdentityNames.GenerateInitials(expectedName));
        }
    }

    [Fact]
    public void GenerateInitials_Cases()
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(ResolveGolden()));
        foreach (var entry in doc.RootElement.GetProperty("initials").EnumerateArray())
        {
            var name = entry.GetProperty("name").GetString()!;
            var expected = entry.GetProperty("initials").GetString()!;
            Assert.Equal(expected, IdentityNames.GenerateInitials(name));
        }
    }

    [Fact]
    public void GenerateColor_SkipsTaken()
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(ResolveGolden()));
        var entry = doc.RootElement.GetProperty("color_taken");
        var id = entry.GetProperty("id").GetString()!;
        var taken = entry.GetProperty("taken").EnumerateArray().Select(e => e.GetString()!).ToHashSet();
        var expected = entry.GetProperty("color").GetString()!;
        Assert.Equal(expected, IdentityNames.GenerateColor(id, taken));
    }
}
