//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Tests;

internal static class TestData
{
    public static string PathTo(params string[] parts)
    {
        var baseDir = AppContext.BaseDirectory;
        var candidate = System.IO.Path.Combine(new[] { baseDir, "testdata" }.Concat(parts).ToArray());
        if (File.Exists(candidate))
        {
            return candidate;
        }

        var dir = new DirectoryInfo(baseDir);
        while (dir is not null)
        {
            candidate = System.IO.Path.Combine(new[] { dir.FullName, "testdata" }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }

            candidate = System.IO.Path.Combine(
                new[] { dir.FullName, "tests", "Provide.Uterm.Tests", "testdata" }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }

            candidate = System.IO.Path.Combine(
                new[] { dir.FullName, "packages", "provide-uterm-csharp", "tests", "Provide.Uterm.Tests", "testdata" }
                    .Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }

            dir = dir.Parent;
        }

        return System.IO.Path.Combine(new[] { baseDir, "testdata" }.Concat(parts).ToArray());
    }
}
